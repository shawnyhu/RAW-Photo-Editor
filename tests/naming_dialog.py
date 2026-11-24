"""Verify project-based export filenames and overwrite choices in the actual file selector.

blender --factory-startup --enable-event-simulate --python tests/naming_dialog.py -- A.dng B.arw
Set BLENDERRAW_TEST_WHEEL_SITE to the installed extension's wheel directory when testing from source.
"""
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if wheel_site := os.environ.get("BLENDERRAW_TEST_WHEEL_SITE"):
    sys.path.append(wheel_site)
import blender_raw as extension
from blender_raw import naming


def wait_until(check, timeout=120):
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "Timed out waiting for the dialog or background export"
        yield


def test():
    extension.register()
    bpy.context.preferences.view.show_splash = False
    parent = bpy.context.window_manager.windows[0]
    parent.event_simulate(type="ESC", value="PRESS")
    parent.event_simulate(type="ESC", value="RELEASE")
    yield
    area = max(parent.screen.areas, key=lambda item: item.width * item.height)
    area.ui_type = extension.gallery.GALLERY_TYPE
    region = next(r for r in area.regions if r.type == "WINDOW")
    source_paths = [Path(s).resolve() for s in sys.argv[sys.argv.index("--") + 1:]]
    assert len(source_paths) == 2
    for path in source_paths:
        photo, _ = extension.catalog.add_photo(bpy.context, path)
        photo.selected = True
    lib = extension.catalog.library(bpy.context)
    originals = [(p.name, p.filepath) for p in lib.photos]
    parent.scene.render.image_settings.file_format = "JPEG"
    layouts = []
    draw = naming.draw

    def inspect_draw(layout, context, photos, library, directory):
        draw(layout, context, photos, library, directory)
        if context.area.type == 'FILE_BROWSER':
            layouts.append(str(layout.introspect()))

    naming.draw = inspect_draw
    with tempfile.TemporaryDirectory(prefix="BlenderRaw-live-names-") as scratch:
        target = Path(scratch)
        for enabled, overwrite in ((True, False), (True, True), (False, False), (False, True)):
            if enabled and overwrite:
                project = target / "Field 日记.v2.blend"
                assert bpy.ops.wm.save_as_mainfile(filepath=str(project)) == {"FINISHED"}
                originals = [(p.name, p.filepath) for p in lib.photos]
            project_stem = Path(bpy.data.filepath).stem or "Untitled"
            stems = [p.stem for p in source_paths]
            names = ([f"{project_stem}_{stem}_{i:03d}.jpg" for i, stem in enumerate(stems, 1)]
                     if enabled else [stem + ".jpg" for stem in stems])
            occupied = target / names[0]
            occupied.write_bytes(b"previous export")
            expected = list(names)
            if not overwrite:
                expected[0] = occupied.stem + "-2.jpg"
            layouts.clear()
            with bpy.context.temp_override(window=parent, area=area, region=region):
                assert bpy.ops.blender_raw.export_photos('INVOKE_DEFAULT', directory=scratch + os.sep) == {'RUNNING_MODAL'}
            yield from wait_until(lambda: any(a.type == "FILE_BROWSER" for w in bpy.context.window_manager.windows for a in w.screen.areas))
            browser, dialog = next((w, a) for w in bpy.context.window_manager.windows
                                   for a in w.screen.areas if a.type == "FILE_BROWSER")
            space = dialog.spaces.active
            space.show_region_tool_props = True
            canvas = next(r for r in dialog.regions if r.type == "WINDOW")
            yield from wait_until(lambda: layouts and "Batch Rename" in layouts[-1])
            assert Path(os.fsdecode(space.params.directory)).samefile(target)
            lib.rename_exports, lib.overwrite_exports = enabled, overwrite
            dialog.tag_redraw()
            yield from wait_until(lambda: "Overwrite" in layouts[-1])
            assert all(label not in layouts[-1] for label in ("Add Name Rule", "Name Template", "Start Number", "Digits"))
            yield from wait_until(lambda: all(name in layouts[-1] for name in expected))
            with bpy.context.temp_override(window=browser, area=dialog, region=canvas):
                assert bpy.ops.file.execute() == {'FINISHED'}
            yield from wait_until(lambda: extension.jobs.export_running())
            yield from wait_until(lambda: not extension.jobs.export_running(), timeout=300)
            assert extension.jobs.export_status() == "Export complete. 2 saved, 0 failed.", extension.jobs.export_details()
            for name in expected:
                assert (target / name).stat().st_size > 1000
                with (target / name).open("rb") as result:
                    assert result.read(2) == b"\xff\xd8"
            if not overwrite:
                assert occupied.read_bytes() == b"previous export"
            assert [(p.name, p.filepath) for p in lib.photos] == originals
            print("DIALOG FILENAMES", "rename", enabled, "overwrite", overwrite, expected, flush=True)
    naming.draw = draw
    area.ui_type = "CompositorNodeTree"
    yield
    extension.unregister()


steps = test()


def advance():
    try:
        next(steps)
        return .1
    except StopIteration:
        print("Live file-selector naming regression passed", flush=True)
    except Exception:
        traceback.print_exc()
        sys.stderr.flush()
        os._exit(1)
    bpy.ops.wm.quit_blender()


bpy.app.timers.register(advance, first_interval=1)
