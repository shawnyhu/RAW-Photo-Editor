"""Check actual Viewer pixels after Gallery clicks, without touching effect cards.

blender --factory-startup --enable-event-simulate -p 50 50 1600 1000 -P tests/native_photo_switch_ui.py
"""
from pathlib import Path
import sys
import tempfile
import time
import traceback

import bpy
import numpy as np
import OpenImageIO as oiio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import blender_raw
from blender_raw import catalog, compositor, gallery, jobs, native_effects

scratch = tempfile.TemporaryDirectory(prefix='native-photo-switch-')
folder = Path(scratch.name)
catalog.cache_dir = lambda: folder
completed = []
# Blender's automatic GPU precision uses half-float intermediates.
PIXEL_TOLERANCE = 2e-3 if '--gpu' in sys.argv else 2e-5


def post(scene, *_):
    completed.append(scene.get(compositor.PHOTO_TAG))


def write(path, color):
    pixels = np.tile([*color, 1.0], (48, 64, 1)).astype(np.float32)
    output = oiio.ImageOutput.create(str(path))
    assert output.open(str(path), oiio.ImageSpec(64, 48, 4, oiio.FLOAT))
    assert output.write_image(pixels)
    output.close()


def photograph(name, color):
    photo = catalog.library(bpy.context).photos.add()
    photo.uid = photo.name = name
    photo.width, photo.height = 64, 48
    catalog.set_camera_input(photo, 0)
    source = folder / (name + '.exr')
    write(source, color)
    photo.filepath = photo.source_absolute = str(source)
    photo.thumbnail_path = str(source)
    photo['thumbnail_bytes'] = source.read_bytes()
    req = catalog.request(photo, 'preview')
    write(Path(req['output']), color)
    catalog.apply_image(photo, req)
    scene = compositor.ensure_photo_scene(bpy.context, photo)
    scene.render.compositor_device = 'GPU' if '--gpu' in sys.argv else 'CPU'
    return photo


def wait_for(predicate, message, seconds=10):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if predicate():
            return
        yield
    raise AssertionError(message)


def pixels():
    viewer = compositor.viewer_image()
    if not viewer.has_data or tuple(viewer.size) != (64, 48):
        return None
    data = np.empty(64 * 48 * 4, dtype=np.float32)
    viewer.pixels.foreach_get(data)
    return data.reshape(-1, 4)[64 * 24 + 32, :3]


def click_photo(window, name):
    area = next(a for a in window.screen.areas if a.ui_type == gallery.GALLERY_TYPE)
    region = next(r for r in area.regions if r.type == 'WINDOW')
    with bpy.context.temp_override(window=window, area=area, region=region):
        shown = gallery._filtered(catalog.library(bpy.context))
        grid = gallery._layout(bpy.context, shown)
        _, _, x, y = next(item for item in gallery._visible_cards(shown, grid) if item[1].uid == name)
        x, y = round(region.x + x + grid.card_w / 2), round(region.y + y + grid.card_h / 2)
    for kind, value in (('MOUSEMOVE', 'NOTHING'), ('LEFTMOUSE', 'PRESS'), ('LEFTMOUSE', 'RELEASE')):
        window.event_simulate(type=kind, value=value, x=x, y=y)
        yield


def test():
    assert not bpy.app.background
    blender_raw.register()
    bpy.app.handlers.composite_post.append(post)
    window = bpy.context.window_manager.windows[0]
    bpy.context.preferences.view.show_splash = False
    window.event_simulate(type='ESC', value='PRESS')
    window.event_simulate(type='ESC', value='RELEASE')
    yield
    window.scene.blender_raw.owner = True
    colors = {'Photo A': (.6, .03, .02), 'Photo B': (.02, .5, .04)}
    photos = {name: photograph(name, color) for name, color in colors.items()}
    catalog.library(bpy.context).active_index = 0
    with bpy.context.temp_override(window=window, area=window.screen.areas[0]):
        assert bpy.ops.blender_raw.new_workspace() == {'FINISHED'}
    yield from wait_for(lambda: window.workspace.get('blender_raw_workspace') and not gallery._timers,
                       'Workspace did not finish')
    for name in ('Photo A', 'Photo B', 'Photo A', 'Photo B'):
        selected = photos[name].scene.compositor_effects.active
        yield from click_photo(window, name)
        yield from wait_for(lambda: window.scene == photos[name].scene, 'Gallery did not switch the Scene')
        try:
            yield from wait_for(lambda: pixels() is not None and np.allclose(pixels(), colors[name], atol=PIXEL_TOLERANCE),
                               f'Viewer stayed stale after selecting {name}')
        except AssertionError:
            print('STALE_VIEWER', name, 'actual', pixels(), 'composite_post', completed, flush=True)
            raise
        assert photos[name].scene.compositor_effects.active == selected
        print('VIEWER_MATCH', name, pixels(), flush=True)

    # The refresh must retain each Scene's active card and still show its full
    # stack, including adjustments downstream of the selected Reader.
    scene = photos['Photo A'].scene
    native_effects.set_input_value(scene.compositor_effects[1], 'Exposure', 1)
    scene.compositor_effects.active = scene.compositor_effects[0]
    colors['Photo A'] = tuple(value * 2 for value in colors['Photo A'])
    for name in ('Photo A', 'Photo B', 'Photo A'):
        selected = photos[name].scene.compositor_effects.active
        yield from click_photo(window, name)
        yield from wait_for(lambda: pixels() is not None and np.allclose(pixels(), colors[name], atol=PIXEL_TOLERANCE),
                           f'Adjusted stack did not appear for {name}')
        assert photos[name].scene.compositor_effects.active == selected

    # Coalesce multiple switches before the next UI/compositor tick. Only the
    # final choice may remain in the shared native Viewer image.
    area = next(a for a in window.screen.areas if a.ui_type == gallery.GALLERY_TYPE)
    region = next(r for r in area.regions if r.type == 'WINDOW')
    with bpy.context.temp_override(window=window, area=area, region=region):
        for name in ('Photo B', 'Photo A', 'Photo B'):
            assert bpy.ops.blender_raw.open_photo(uid=name) == {'FINISHED'}
    yield from wait_for(lambda: window.scene == photos['Photo B'].scene and pixels() is not None
                       and np.allclose(pixels(), colors['Photo B'], atol=PIXEL_TOLERANCE),
                       'Rapid switches left a previous photo in the Viewer')
    for _ in range(5):
        yield
    count = len(completed)
    for _ in range(5):
        yield
    assert len(completed) == count, 'Idle Viewer kept recompositing after the switch'
    print('NATIVE_PHOTO_SWITCH_UI_PASS:', window.scene.render.compositor_device,
          'Gallery clicks update cached pixels, retain card selection and downstream edits; rapid switches settle without idle recompositing', flush=True)


steps = test()


def tick():
    try:
        next(steps)
        return .2
    except StopIteration:
        pass
    except BaseException:
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
    bpy.app.handlers.composite_post.remove(post)
    jobs.stop_previews()
    scratch.cleanup()
    bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(tick, first_interval=1)
