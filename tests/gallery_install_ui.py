"""Run in a fresh, isolated Blender window with --enable-event-simulate.

blender --offline-mode --python tests/gallery_install_ui.py -- PACKAGE.zip PHOTO.dng [YYYY-MM-DD]
"""
import importlib
import os
from pathlib import Path
import sys
import time
import traceback

import bpy


def wait_for(condition, message):
    deadline = time.monotonic() + 90
    while not condition():
        if time.monotonic() >= deadline:
            raise AssertionError(message)
        yield


def run():
    package, source, *dates = sys.argv[sys.argv.index("--") + 1:]
    assert not hasattr(bpy.types.Scene, "blender_raw"), "Use a fresh Blender profile"
    window = bpy.context.window_manager.windows[0]
    bpy.context.preferences.view.show_splash = False
    window.event_simulate(type="ESC", value="PRESS")
    window.event_simulate(type="ESC", value="RELEASE")
    yield
    result = bpy.ops.extensions.package_install_files(
        filepath=str(Path(package).resolve()), repo="user_default", enable_on_install=True)
    assert result == {"FINISHED"}, result
    ext = importlib.import_module("bl_ext.user_default.raw_photo_editor")
    with bpy.context.temp_override(window=window, area=window.screen.areas[0]):
        assert bpy.ops.blender_raw.open_workspace() == {"FINISHED"}
    yield from wait_for(lambda: any(a.ui_type == ext.gallery.GALLERY_TYPE for a in window.screen.areas),
                        "Photo Gallery did not open")
    area = next(a for a in window.screen.areas if a.ui_type == ext.gallery.GALLERY_TYPE)
    region = next(r for r in area.regions if r.type == "WINDOW")
    with bpy.context.temp_override(window=window, area=area, region=region):
        assert bpy.ops.blender_raw.import_photos(filepath=str(Path(source).resolve())) == {"FINISHED"}
        lib = ext.catalog.library(bpy.context)
        photo, = lib.photos
    yield from wait_for(lambda: photo.thumbnail_path or photo.get("thumbnail_error"), "Thumbnail timed out")
    assert photo.thumbnail_path and not photo.tree, photo.get("thumbnail_error")
    with bpy.context.temp_override(window=window, area=area, region=region):
        shown = ext.gallery._filtered(lib)
        layout = ext.gallery._layout(bpy.context, shown)
        _, _, left, bottom = next(ext.gallery._visible_cards(shown, layout))
        point = dict(x=int(region.x + left + layout.card_w / 2),
                     y=int(region.y + bottom + layout.card_h / 2))
        card_rect = (left, bottom, layout.card_w, layout.card_h)
    borders = []
    draw_rectangle = ext.gallery._rectangle
    def record_border(shader, rect, color):
        if tuple(rect) == card_rect:
            borders.append(color[:3])
        draw_rectangle(shader, rect, color)
    ext.gallery._rectangle = record_border
    def border_matches(predicate):
        borders.clear()
        area.tag_redraw()
        yield from wait_for(lambda: borders and predicate(*borders[-1]), "Unexpected Gallery border")

    # The initial keyboard index points at the import before the Viewer shows it.
    yield from border_matches(lambda r, g, b: max(r, g, b) - min(r, g, b) < .1)
    photo.selected = True
    yield from border_matches(lambda r, g, b: b > g > r and b - r > .2)
    window.event_simulate(type="MOUSEMOVE", value="NOTHING", **point)
    yield
    window.event_simulate(type="LEFTMOUSE", value="PRESS", **point)
    yield
    window.event_simulate(type="LEFTMOUSE", value="RELEASE", **point)
    yield from wait_for(lambda: photo.status == "Ready" or photo.error, "Thumbnail click did not open the photo")
    assert not photo.error, photo.error
    assert photo.selected and photo.tree and photo.image and min(photo.image.size) > 1
    photo.selected = False
    yield from border_matches(lambda r, g, b: g > r and g - b > .2)
    ext.gallery._rectangle = draw_rectangle
    developer, = ext.compositor.developers_for(photo)
    yield from wait_for(lambda: not ext.jobs.developing(photo.uid), "The camera profile preview timed out")
    assert (developer.camera_input, developer.look) == (ext.catalog.DEFAULT_CAMERA_INPUT, "NONE") and not developer.error, developer.error
    developed = developer.node_tree.nodes["Developed RAW"].image
    assert developed and developed.get("photo_editor_profile")
    editor = next(a for a in window.screen.areas if a.type == "IMAGE_EDITOR")
    viewer = editor.spaces.active.image
    assert viewer.type == "COMPOSITING"
    yield from wait_for(lambda: tuple(viewer.size) == tuple(photo.image.size),
                        "The Image Editor did not display the developed preview")
    yield from wait_for(lambda: not ext.gallery.edited_preview._fit_requests, "Viewer did not fit the new image")
    image_region = next(r for r in editor.regions if r.type == "WINDOW")
    with bpy.context.temp_override(window=window, area=editor, region=image_region):
        fitted = tuple(editor.spaces.active.zoom)
        bpy.ops.image.view_all(fit_view=True)
    yield
    with bpy.context.temp_override(window=window, area=editor, region=image_region):
        assert tuple(editor.spaces.active.zoom) == fitted, "Gallery zoom differs from Blender's Fit"
        bpy.ops.image.view_zoom_ratio(ratio=2.0)
    yield
    with bpy.context.temp_override(window=window, area=editor, region=image_region):
        manual_zoom = tuple(editor.spaces.active.zoom)
        assert manual_zoom != fitted, (manual_zoom, fitted)
    developer.lut_path = str(Path(__file__).resolve().parent / "fixtures" / "half-green.cube")
    for look in ("LUT", "NONE"):
        developer.look = look
        yield from wait_for(lambda: not ext.jobs.developing(photo.uid), "Look switch timed out")
        variant = developer.node_tree.nodes["Developed RAW"].image
        assert not developer.error and variant["photo_editor_profile"] == developed["photo_editor_profile"]
        assert developer.camera_input == ext.catalog.DEFAULT_CAMERA_INPUT and (variant == developed) == (look == "NONE")
        with bpy.context.temp_override(window=window, area=editor, region=image_region):
            assert tuple(editor.spaces.active.zoom) == manual_zoom, (editor.spaces.active.zoom[:], manual_zoom)
    if dates:
        yield from wait_for(lambda: photo.capture_date_indexed, "Capture date indexing timed out")
        assert photo.capture_date == dates[0], (photo.capture_date, dates[0])
    print("GALLERY CLICK", photo.name, tuple(photo.image.size), developed["photo_editor_profile"], photo.capture_date, flush=True)


steps = run()


def advance():
    try:
        next(steps)
        return 0.15
    except StopIteration:
        bpy.ops.wm.quit_blender()
    except Exception:
        traceback.print_exc()
        sys.stderr.flush()
        os._exit(1)


bpy.app.timers.register(advance, first_interval=1)
