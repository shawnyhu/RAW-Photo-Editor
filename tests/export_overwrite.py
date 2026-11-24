"""Verify actual files and atomic overwrite behavior in an isolated Blender.

blender -b --python-exit-code 1 --python tests/export_overwrite.py -- PHOTO.dng
"""
import hashlib
import importlib
import os
from pathlib import Path
import sys
import tempfile
import time

import bpy
import OpenImageIO as oiio


if os.environ.get("BLENDERRAW_TEST_FROM_SOURCE"):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    if wheel_site := os.environ.get("BLENDERRAW_TEST_WHEEL_SITE"):
        sys.path.append(wheel_site)
    import blender_raw as pkg
    pkg.register()
else:
    pkg = importlib.import_module("bl_ext.user_default.raw_photo_editor")


def finish(success, failed):
    deadline = time.monotonic() + 300
    while pkg.jobs.export_running():
        pkg.jobs.tick()
        assert time.monotonic() < deadline
        time.sleep(.05)
    assert pkg.jobs.export_status() == f"Export complete. {success} saved, {failed} failed.", pkg.jobs.export_details()


def verify_jpeg(path):
    image = oiio.ImageInput.open(str(path))
    assert image, path
    try:
        assert image.format_name() == "jpeg" and min(image.spec().width, image.spec().height) > 0
    finally:
        image.close()


source = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
with source.open("rb") as stream:
    original_digest = hashlib.file_digest(stream, "sha256").digest()
lib = pkg.catalog.library(bpy.context)
photo, _ = pkg.catalog.add_photo(bpy.context, source)
photo.selected = True
bpy.context.scene.render.image_settings.file_format = "JPEG"
assert not lib.overwrite_exports and not lib.rename_exports

with tempfile.TemporaryDirectory(prefix="BlenderRaw-overwrite-") as directory:
    folder = Path(directory)
    existing = folder / (source.stem + ".jpg")
    original_output = b"previous export"
    existing.write_bytes(original_output)
    assert bpy.ops.blender_raw.export_photos(directory=directory) == {"FINISHED"}
    lib.overwrite_exports = True
    finish(1, 0)
    assert existing.read_bytes() == original_output
    keep_both = folder / (source.stem + "-2.jpg")
    verify_jpeg(keep_both)
    second_digest = hashlib.sha256(keep_both.read_bytes()).digest()

    missing = lib.photos.add()
    missing.uid, missing.name = "broken-raw", "missing.dng"
    missing.filepath, missing.selected = str(folder / missing.name), True
    failed_output = folder / "missing.jpg"
    failed_output.write_bytes(original_output)
    assert bpy.ops.blender_raw.export_photos(directory=directory) == {"FINISHED"}
    lib.overwrite_exports = False
    finish(1, 1)
    verify_jpeg(existing)
    assert failed_output.read_bytes() == original_output
    assert hashlib.sha256(keep_both.read_bytes()).digest() == second_digest
    assert {p.name for p in folder.iterdir()} == {existing.name, keep_both.name, failed_output.name}

    # A competing writer can create the planned destination while Blender is
    # developing the original. Default mode may never replace that file.
    missing.selected = False
    late = folder / "race"
    assert bpy.ops.blender_raw.export_photos(directory=str(late)) == {"FINISHED"}
    (late / existing.name).write_bytes(original_output)
    finish(0, 1)
    assert (late / existing.name).read_bytes() == original_output
    assert len(list(late.iterdir())) == 1

with source.open("rb") as stream:
    assert hashlib.file_digest(stream, "sha256").digest() == original_digest
print("Overwrite, keep both, failed export and late collision passed", flush=True)
