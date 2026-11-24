"""Exercise installed Gallery and the native export settings with a directory of RAWs.

blender -b --python-exit-code 1 --python tests/camera_batch.py -- INPUT_DIR [CR3,ARW,DNG,NEF]
All output is temporary. The source directory is only read.
"""
from collections import Counter
import importlib
from pathlib import Path
import sys
import tempfile
import time

import bpy
import OpenImageIO as oiio
import pyexiv2


pkg = importlib.import_module("bl_ext.user_default.raw_photo_editor")
args = sys.argv[sys.argv.index("--") + 1:]
root = Path(args[0]).resolve()
suffixes = (args[1] if len(args) > 1 else "CR3,ARW,DNG,NEF").lower().split(",")
paths = sorted(p for p in root.iterdir() if p.is_file() and p.suffix[1:].lower() in suffixes)
assert paths, "No RAWs found"


def until(check, timeout=1800):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pkg.jobs.tick()
        if check():
            return
        time.sleep(.1)
    raise AssertionError("BlenderRaw job timed out")


def output_info(path):
    image = oiio.ImageInput.open(str(path))
    assert image, str(path)
    try:
        spec = image.spec()
        return (spec.width, spec.height), spec.format, spec.nchannels
    finally:
        image.close()


def export(folder):
    assert bpy.ops.blender_raw.export_photos(directory=str(folder)) == {"FINISHED"}
    until(lambda: not pkg.jobs.export_running())
    assert not pkg.jobs.export_details(), pkg.jobs.export_details()


with tempfile.TemporaryDirectory(prefix="BlenderRaw-cameras-") as output:
    out = Path(output)
    assert bpy.ops.blender_raw.import_photos(directory=str(root),
        files=[{"name": p.name} for p in paths]) == {"FINISHED"}
    lib = pkg.catalog.library(bpy.context)
    assert len(lib.photos) == len(paths)
    first = lib.photos[0]
    lib.preview_resolution = "100"
    assert not any(pkg.catalog.request(p, "preview")["settings"]["preview"] for p in lib.photos)
    lib.preview_resolution = "PROXY"
    previews = {}
    for photo in lib.photos:
        assert bpy.ops.blender_raw.open_photo(uid=photo.uid) == {"FINISHED"}
        until(lambda: photo.error or (photo.image and photo.status == "Ready" and not pkg.jobs.developing(photo.uid)))
        assert not photo.error, (photo.name, photo.error)
        developer, = pkg.compositor.developers_for(photo)
        assert (developer.camera_input, developer.look) == (pkg.catalog.DEFAULT_CAMERA_INPUT, "NONE") and not developer.error, (photo.name, developer.error)
        developed = developer.node_tree.nodes["Developed RAW"].image
        assert developed and tuple(developed.size) == tuple(photo.image.size)
        assert developed.get("photo_editor_profile"), photo.name
        previews[photo.name] = tuple(photo.image.size)
        assert 1 < max(previews[photo.name]) <= 1600
        photo.selected = True
        print("PREVIEW", photo.name, previews[photo.name], developed["photo_editor_profile"], flush=True)
    until(lambda: all(p.thumbnail_path or p.get("thumbnail_error") for p in lib.photos))
    assert all(p.thumbnail_path for p in lib.photos)

    for photo in lib.photos:
        settings = photo.scene.render.image_settings
        settings.file_format = "JPEG"
        settings.color_mode = "RGB"
        settings.quality = 83
    export(out)
    summary = Counter()
    for photo in lib.photos:
        orig = root / photo.name
        dest = out / (orig.stem + ".jpg")
        size, _, _ = output_info(dest)
        assert max(size) > max(previews[photo.name]), (orig, size)
        with pyexiv2.Image(str(orig)) as raw, pyexiv2.Image(str(dest)) as jpeg:
            source, result = raw.read_exif(), jpeg.read_exif()
            captured = source.get("Exif.Photo.DateTimeOriginal") or source.get("Exif.Image.DateTimeOriginal")
            if captured:
                assert photo.capture_date == captured[:10].replace(":", "-"), (orig, photo.capture_date)
            for key in ("Exif.Image.Make", "Exif.Image.Model", "Exif.Photo.DateTimeOriginal",
                        "Exif.Photo.ExposureTime", "Exif.Photo.FNumber", "Exif.Photo.FocalLength",
                        "Exif.Photo.ISOSpeedRatings", "Exif.Photo.LensModel"):
                if key in source:
                    assert result.get(key) == source[key], (orig, key, result.get(key), source[key])
            assert result.get("Exif.Image.Model"), orig
            summary[(orig.suffix.lower(), result["Exif.Image.Model"])] += 1
        print("EXPORTED", orig.name, size, photo.capture_date, flush=True)
    print("CAMERA MATRIX", dict(summary), flush=True)

    for photo in lib.photos:
        photo.selected = photo.uid == first.uid
    settings = first.scene.render.image_settings
    settings.file_format = "PNG"
    settings.color_depth = "16"
    settings.color_mode = "BW"
    settings.compression = 21
    # The export dialog reads image settings from its invoking Photo Scene.
    with bpy.context.temp_override(scene=first.scene):
        export(out)
    png = out / (Path(first.name).stem + ".png")
    _, dtype, channels = output_info(png)
    assert str(dtype) == "uint16" and channels == 1, (dtype, channels)
    assert (settings.file_format, settings.color_mode, settings.color_depth, settings.compression) == ("PNG", "BW", "16", 21)
    print("NATIVE PNG PASSED", flush=True)
