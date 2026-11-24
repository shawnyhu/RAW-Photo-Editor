"""Run in an isolated Blender 5.3 installation with the built extension enabled.

blender -b --python-exit-code 1 --python tests/gallery_roundtrip.py -- A.dng B.dng
"""
import hashlib
import importlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time

import bpy
import OpenImageIO as oiio
import pyexiv2


pkg = importlib.import_module("bl_ext.user_default.raw_photo_editor")
assert hasattr(bpy.types.Scene, "blender_raw"), "Install and enable the package first"
args = sys.argv[sys.argv.index("--") + 1:]
assert len(args) in {2, 3}, "Pass two camera RAW files and optionally a DCP for the first"


def wait_for(predicate, message, timeout=400):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        pkg.jobs.tick()
        time.sleep(.1)
    assert predicate(), message


def dimensions(path):
    src = oiio.ImageInput.open(str(path))
    assert src, f"Could not read {path}"
    spec = src.spec()
    result = (spec.width, spec.height)
    src.close()
    return result


with tempfile.TemporaryDirectory(prefix="BlenderRaw-smoke-") as scratch:
    folder = Path(scratch)
    photo_ids = []
    local_originals = []
    stamps = []
    for i, arg in enumerate(args[:2]):
        source = Path(arg)
        local = folder / f"Source {i}{source.suffix}"
        shutil.copyfile(source, local)
        local_originals.append(local)
        stamps.append(hashlib.sha256(local.read_bytes()).digest())
        assert bpy.ops.blender_raw.import_photos(filepath=str(local), pack=True) == {"FINISHED"}
    lib = pkg.catalog.library(bpy.context)
    assert len(lib.photos) == 2 and all(not photo.tree for photo in lib.photos)
    if len(args) == 3:
        dcp = folder / "Input Profile.dcp"
        shutil.copyfile(args[2], dcp)
        first = lib.photos[0]
        first.profile_path = str(dcp)
        first.camera_input = "DCP"
        first.look = "PROFILE"
        pkg.catalog.pack_photo(first)
        dcp.unlink()
    for index, photo in enumerate(lib.photos):
        photo_ids.append(photo.uid)
        photo.rating = 3 + index
        photo.flagged = index == 0
        photo.tags = "Roundtrip, Color"
        photo.selected = True
        assert bpy.ops.blender_raw.open_photo(uid=photo.uid) == {"FINISHED"}
        wait_for(lambda: (photo.image and photo.status == "Ready") or photo.error, photo.name)
        assert not photo.error, photo.error
        node, = pkg.compositor.developers_for(photo)
        if index == 1:
            # 0.10.1 encoded BaseLook as zero or omitted the setting entirely.
            del node["photo_editor_look"]
            photo["photo_editor_look"] = 0
            assert photo.look == node.look == "BASELOOK"
        assert photo.image.colorspace_settings.name == "Linear Rec.709"
        assert photo.tree.nodes.get("Group Output")
        print("PREVIEW", photo.name, photo.image.size[:], flush=True)
    wait_for(lambda: all(p.thumbnail_path or p.get("thumbnail_error") for p in lib.photos), "Missing thumbnails")
    assert all(p.thumbnail_path for p in lib.photos)
    assert lib.photos[0].tree != lib.photos[1].tree
    tree = lib.photos[0].tree
    output_node = next(node for node in tree.nodes if node.bl_idname == "NodeGroupOutput")
    source_socket = output_node.inputs["Image"].links[0].from_socket
    grade = tree.nodes.new("CompositorNodeExposure")
    grade.label = "My Grade"
    grade.inputs["Exposure"].default_value = 1
    tree.links.new(source_socket, grade.inputs["Image"])
    tree.links.new(grade.outputs[0], output_node.inputs["Image"])
    blendfile = folder / "Roundtrip.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(blendfile))
    for local in local_originals:
        local.unlink()
    bpy.ops.wm.open_mainfile(filepath=str(blendfile))
    lib = pkg.catalog.library(bpy.context)
    for index, photo in enumerate(lib.photos):
        node, = pkg.compositor.developers_for(photo)
        expected_look = "BASELOOK" if index else ("PROFILE" if len(args) == 3 else "NONE")
        assert node.look == expected_look and node.camera_input == ("DCP" if expected_look == "PROFILE" else pkg.catalog.DEFAULT_CAMERA_INPUT)
        assert photo.look == expected_look
        assert photo.uid == photo_ids[index] and photo.rating == 3 + index
        assert photo.flagged == (index == 0)
        assert hashlib.sha256(pkg.catalog.source_file(photo).read_bytes()).digest() == stamps[index]
    assert any(n.label == "My Grade" for n in lib.photos[0].tree.nodes)
    output = folder / "Export"
    output.mkdir()
    (output / "Source 0.jpg").write_bytes(b"existing file")
    assert bpy.ops.blender_raw.export_photos(directory=str(output), format="JPEG") == {"FINISHED"}
    wait_for(lambda: not pkg.jobs.export_running(), "Export timeout")
    assert not pkg.jobs.export_details(), pkg.jobs.export_details()
    assert (output / "Source 0.jpg").read_bytes() == b"existing file"
    files = [output / "Source 0-2.jpg", output / "Source 1.jpg"]
    sizes = [dimensions(path) for path in files]
    for source, result in zip(args[:2], files):
        with pyexiv2.Image(str(source)) as original, pyexiv2.Image(str(result)) as jpeg:
            exif, inherited = original.read_exif(), jpeg.read_exif()
            for key in ("Exif.Image.Model", "Exif.Photo.DateTimeOriginal", "Exif.Photo.FocalLength",
                        "Exif.Photo.LensModel", "Exif.Photo.ExposureTime", "Exif.Photo.FNumber",
                        "Exif.Photo.ISOSpeedRatings"):
                if key in exif:
                    assert inherited.get(key) == exif[key], (source, key)
    print("ROUNDTRIP", json.dumps({"sizes": sizes, "result": pkg.jobs.export_status()}), flush=True)
    assert all(w > 1 and h > 1 for w, h in sizes)
    assert len({p.uid for p in lib.photos}) == 2
    linked = folder / "Missing.dng"
    shutil.copyfile(args[0], linked)
    assert bpy.ops.blender_raw.import_photos(filepath=str(linked), pack=False) == {"FINISHED"}
    new_project = folder / "Moved Project"
    new_project.mkdir()
    bpy.ops.wm.save_as_mainfile(filepath=str(new_project / "Roundtrip.blend"))
    assert pkg.catalog.source_file(lib.photos[2]).samefile(linked)
    lib.photos[2].selected = True
    lib.photos[1].selected = False
    linked.unlink()
    assert bpy.ops.blender_raw.export_photos(directory=str(output), format="TIFF") == {"FINISHED"}
    wait_for(lambda: not pkg.jobs.export_running(), "Mixed batch timeout")
    assert len(pkg.jobs.export_details()) == 1 and "Missing" in pkg.jobs.export_details()[0]
    assert dimensions(output / "Source 0.tif") == sizes[0]
    print("MIXED BATCH", pkg.jobs.export_status(), flush=True)
