"""Blender 5.3 foreground Viewer regression. Pass a Z 8 NEF with an 8280 px long edge.

blender --factory-startup --enable-event-simulate --python tests/oversized_preview.py -- photo.nef
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback

import bpy


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if os.environ.get("BLENDERRAW_TEST_WHEEL_SITE"):
    sys.path.append(os.environ["BLENDERRAW_TEST_WHEEL_SITE"])
installed = "bl_ext.user_default.raw_photo_editor" in bpy.context.preferences.addons
if installed:
    from bl_ext.user_default import raw_photo_editor as extension
else:
    import blender_raw as extension


source = Path(sys.argv[sys.argv.index("--") + 1]).resolve()


def run():
    if not installed:
        extension.register()
    bpy.context.preferences.view.show_splash = False
    window = bpy.context.window_manager.windows[0]
    window.event_simulate(type="ESC", value="PRESS")
    window.event_simulate(type="ESC", value="RELEASE")
    yield
    areas = sorted(window.screen.areas, key=lambda area: area.width * area.height, reverse=True)
    viewer_area, node_area = areas[:2]
    viewer_area.type = "IMAGE_EDITOR"
    node_area.ui_type = "CompositorNodeTree"
    yield
    ordinary = window.scene
    ordinary_name = ordinary.name
    ordinary.render.resolution_x = 9999
    ordinary.render.compositor_device = "GPU"
    canvas = next(region for region in node_area.regions if region.type == "WINDOW")
    with bpy.context.temp_override(window=window, area=node_area, region=canvas):
        lib = extension.catalog.library(bpy.context)
        modes = lib.bl_rna.properties["preview_resolution"].enum_items
        assert [(item.name, item.value) for item in modes] == [("Proxy", 0), ("Half", 1), ("Full", 3)]
        photo, _ = extension.catalog.add_photo(bpy.context, source)
        uid = photo.uid
        lib.preview_resolution = "100"
        assert bpy.ops.blender_raw.open_photo(uid=uid) == {"FINISHED"}
    studio = window.scene
    studio.render.compositor_device = "GPU"
    for resolution, target_edge, expected_device in (("100", 8280, "CPU"), ("50", 4140, "GPU"), ("PROXY", 1600, "GPU")):
        lib.preview_resolution = resolution
        deadline = time.monotonic() + 180
        while True:
            viewer = viewer_area.spaces.active.image
            assert not photo.error, photo.error
            if (photo.status == "Ready" and photo.image and max(photo.image.size) == target_edge
                    and viewer and viewer.type == "COMPOSITING" and viewer.has_data
                    and tuple(viewer.size) == tuple(photo.image.size)
                    and not extension.jobs.developing(photo.uid) and not bpy.app.is_job_running("COMPOSITE")):
                mid = 4 * (viewer.size[1] // 2 * viewer.size[0] + viewer.size[0] // 2)
                if max(viewer.pixels[mid:mid + 3]) > 0:
                    break
            assert time.monotonic() < deadline, (resolution, photo.status, studio.render.compositor_device,
                                                 tuple(viewer.size) if viewer else None)
            yield
        assert studio.render.compositor_device == expected_device
        assert ordinary.render.compositor_device == "GPU"
        print("VIEWER", resolution, tuple(viewer.size), expected_device, flush=True)

    # A saved project from before the fallback may already have an 8280 px GPU darkroom.
    # Check its device immediately on load, before the background decoder can run.
    with tempfile.TemporaryDirectory(prefix="BlenderRaw-device-") as temp:
        saved = str(Path(temp) / "legacy.blend")
        lib["preview_resolution"] = 2  # Enum value written by 0.15.9 for 75%.
        assert bpy.ops.wm.save_as_mainfile(filepath=saved, copy=True) == {"FINISHED"}
        lib.preview_resolution = "PROXY"
        # Produce the legacy fixture offscreen to avoid deliberately provoking the
        # old error while this live window is watching the darkroom.
        subprocess.run([bpy.app.binary_path, "--background", "--factory-startup",
                        "--disable-autoexec", saved, "--python-exit-code", "1", "--python-expr",
                        "import bpy; s=bpy.context.scene; assert s.get('blenderraw_darkroom'); "
                        "s.render.resolution_y=8280; s.render.compositor_device='GPU'; "
                        "bpy.ops.wm.save_as_mainfile(filepath=bpy.data.filepath)"], check=True)
        assert bpy.ops.wm.open_mainfile(filepath=saved) == {"FINISHED"}
        assert extension.catalog.library(bpy.context).preview_resolution == "50"
        studio = bpy.context.window_manager.windows[0].scene
        assert studio.render.compositor_device == "CPU"
        assert bpy.data.scenes[ordinary_name].render.compositor_device == "GPU"
        # Explicit CPU settings survive both sides of Blender's exact cutoff.
        studio.render.resolution_y = 8192
        extension.compositor.restore_preview_devices()
        assert studio.render.compositor_device == "GPU"
        studio.render.compositor_device = "CPU"
        for edge in (8193, 8192):
            studio.render.resolution_y = edge
            extension.compositor.restore_preview_devices()
            assert studio.render.compositor_device == "CPU"
    extension.jobs.stop_previews()
    print("Legacy darkroom and ordinary scene device checks passed", flush=True)


steps = run()


def advance():
    try:
        next(steps)
        return .2
    except StopIteration:
        bpy.ops.wm.quit_blender()
    except Exception:
        traceback.print_exc()
        sys.stderr.flush()
        os._exit(1)


bpy.app.timers.register(advance, first_interval=1, persistent=True)
