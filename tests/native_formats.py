"""Inspect actual Blender output formats with a synthetic RGBA compositor source.

blender -b --factory-startup --python-exit-code 1 --python tests/native_formats.py
"""
from pathlib import Path
from contextlib import closing
import sys
import tempfile
import uuid

import bpy
import numpy as np
import OpenImageIO as oiio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import blender_raw
from blender_raw import catalog, compositor


def inspect(path):
    with closing(oiio.ImageInput.open(str(path))) as image:
        parts = []
        index = 0
        while image.seek_subimage(index, 0):
            spec = image.spec()
            parts.append((spec.width, spec.height, spec.channelnames, str(spec.format),
                          np.asarray(image.read_image(format=oiio.FLOAT))[5, 5].tolist(),
                          spec.get_string_attribute("compression")))
            index += 1
    return parts


def run():
    blender_raw.register()
    scene = bpy.context.scene
    scene.view_settings.view_transform = "Standard"
    image = bpy.data.images.new("RGBA test", width=32, height=24, alpha=True, float_buffer=True)
    image.colorspace_settings.name = "Linear Rec.709"
    image.pixels.foreach_set(np.tile([.16, .08, .04, .5], (32 * 24, 1)).astype("float32").reshape(-1))
    image.update()
    photo = catalog.library(bpy.context).photos.add()
    photo.uid, photo.name, photo.image = uuid.uuid4().hex, "Native export", image
    compositor.ensure_tree(bpy.context, photo)
    settings = scene.render.image_settings
    with tempfile.TemporaryDirectory() as temp:
        cases = [("PNG", "16", "RGBA"), ("WEBP", "8", "RGB"),
                 ("OPEN_EXR", "16", "RGBA"), ("OPEN_EXR", "32", "RGBA"),
                 ("OPEN_EXR_MULTILAYER", "32", "RGBA")]
        for fmt, depth, mode in cases:
            print("CASE", fmt, depth, mode, flush=True)
            settings.media_type = "MULTI_LAYER_IMAGE" if fmt == "OPEN_EXR_MULTILAYER" else "IMAGE"
            settings.file_format = fmt
            settings.color_mode = mode
            settings.color_depth = depth
            if fmt.startswith("OPEN_EXR"):
                settings.exr_codec = "PIZ"
            export_scene = compositor.export_scene(scene)
            outfile = Path(temp) / (fmt + depth + scene.render.file_extension)
            compositor.export_photo(bpy.context, photo, str(outfile), export_scene)
            parts = inspect(outfile)
            print("OUTPUT", fmt, parts, flush=True)
            assert all(item[:2] == (32, 24) for item in parts)
            if fmt.startswith("OPEN_EXR"):
                assert parts
                assert parts[0][3] == ("half" if depth == "16" else "float")
                assert "A" in parts[0][2] or any(ch.endswith(".A") for ch in parts[0][2])
                assert parts[0][-1].lower() == "piz"
                assert any(abs(pixel - .5) < .001 for pixel in parts[0][4])
            elif fmt == "PNG":
                assert parts[0][2] == ("R", "G", "B", "A") and parts[0][3] == "uint16"
                assert abs(parts[0][4][3] - .5) < .001
            elif fmt == "WEBP":
                assert len(parts[0][2]) == 3
            bpy.data.scenes.remove(export_scene)
    blender_raw.unregister()


if __name__ == "__main__":
    run()
