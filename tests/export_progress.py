"""Check live batch progress in Blender with two real RAW inputs; all outputs are temporary.

blender -b --python-exit-code 1 --python tests/export_progress.py -- FIRST_RAW SECOND_RAW
"""
import importlib
import os
from pathlib import Path
import sys
import tempfile
import time

import bpy


if os.environ.get("BLENDERRAW_TEST_FROM_SOURCE"):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    if wheel_site := os.environ.get("BLENDERRAW_TEST_WHEEL_SITE"):
        sys.path.append(wheel_site)
    import blender_raw as pkg
    pkg.register()
else:
    pkg = importlib.import_module("bl_ext.user_default.raw_photo_editor")

jobs = pkg.jobs
inputs = [Path(value).resolve() for value in sys.argv[sys.argv.index("--") + 1:]]
assert len(inputs) == 2 and all(p.is_file() for p in inputs)


with tempfile.TemporaryDirectory(prefix="BlenderRaw-progress-") as directory:
    destination = Path(directory)
    lib = pkg.catalog.library(bpy.context)
    first, _ = pkg.catalog.add_photo(bpy.context, inputs[0])
    missing = lib.photos.add()
    missing.name, missing.uid = "Missing.dng", "missing"
    missing.filepath = str(destination / missing.name)
    second, _ = pkg.catalog.add_photo(bpy.context, inputs[1])
    for photo in lib.photos:
        photo.selected = True
    bpy.context.scene.render.image_settings.file_format = "JPEG"

    assert jobs.export_progress() is None
    try:
        jobs.start_export(bpy.context, [], destination)
    except ValueError:
        pass
    else:
        raise AssertionError("An empty selection started an export")
    assert jobs.export_progress() is None

    assert bpy.ops.blender_raw.export_photos(directory=directory) == {"FINISHED"}
    assert jobs.export_progress() == (0, 3)
    previous = 0
    observed = []
    deadline = time.monotonic() + 300
    while jobs.export_running():
        jobs.tick()
        sample = jobs.export_progress()
        if sample is not None:
            completed, total = sample
            assert total == 3 and previous <= completed <= total
            previous = completed
            status = jobs.export_status()
            if not observed or observed[-1] != (completed, status):
                observed.append((completed, status))
                print("PROGRESS", sample, status, flush=True)
        assert time.monotonic() < deadline
        time.sleep(.02)
    assert any(0 < count < 3 for count, _ in observed), observed
    assert any("Rendering" in status for _, status in observed), observed
    assert any(second.name in status for _, status in observed), observed
    assert jobs.export_progress() is None
    assert jobs.export_status() == "Export complete. 2 saved, 1 failed.", jobs.export_status()
    assert len(jobs.export_details()) == 1 and missing.name in jobs.export_details()[0]
    assert len(list(destination.glob("*.jpg"))) == 2

    assert bpy.ops.blender_raw.export_photos(directory=directory) == {"FINISHED"}
    assert jobs.export_progress() == (0, 3)
    assert bpy.ops.blender_raw.cancel_export() == {"FINISHED"}
    assert jobs.export_progress() is None
    assert jobs.export_status() == "Export cancelled. 0 / 3 saved.", jobs.export_status()
    assert len(list(destination.glob("*.jpg"))) == 2
    print("Export progress, completion, error reporting and cancellation passed.", flush=True)
