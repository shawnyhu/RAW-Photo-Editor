"""Isolated foreground regression: blender --factory-startup --enable-event-simulate --python tests/picker_view_ui.py -- photo.dng"""
import os
from pathlib import Path
import sys
import time
import traceback

import bpy


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if os.environ.get("BLENDERRAW_TEST_WHEEL_SITE"):
    sys.path.append(os.environ["BLENDERRAW_TEST_WHEEL_SITE"])
import blender_raw as extension


bpy.context.preferences.view.show_splash = False
source = Path(sys.argv[sys.argv.index("--") + 1]).resolve()


def run():
    extension.register()
    window = bpy.context.window_manager.windows[0]
    window.event_simulate(type='ESC', value='PRESS')
    window.event_simulate(type='ESC', value='RELEASE')
    yield
    by_size = sorted(window.screen.areas, key=lambda a: a.width * a.height, reverse=True)
    viewer_area, nodes = by_size[:2]
    viewer_area.type = 'IMAGE_EDITOR'
    nodes.ui_type = 'CompositorNodeTree'
    yield
    def override(area):
        return bpy.context.temp_override(window=window, area=area,
                    region=next(r for r in area.regions if r.type == 'WINDOW'))
    with override(nodes):
        photo, _ = extension.catalog.add_photo(bpy.context, source)
        uid = photo.uid
        extension.compositor.open_photo(bpy.context, photo)
        developer = photo.tree.nodes.active
        reader = next(n for n in photo.tree.nodes if n.bl_idname == extension.compositor.READER_TYPE)
        other = photo.tree.nodes.new(extension.compositor.DEVELOPER_TYPE)
        photo.tree.links.new(reader.outputs[0], other.inputs[0])
        window.scene.render.compositor_device = 'CPU'
        extension.jobs.preview_later(photo, immediate=True)
    deadline = time.monotonic() + 90
    while not (photo.decode_key and photo.status == 'Ready' and photo.image
               and viewer_area.spaces.active.image and viewer_area.spaces.active.image.has_data
               and viewer_area.spaces.active.image.size[:] == photo.image.size[:]):
        assert not photo.error, photo.error
        assert time.monotonic() < deadline, "The compositor Viewer did not receive the photo"
        yield
    space = viewer_area.spaces.active
    canvas = next(r for r in viewer_area.regions if r.type == 'WINDOW')
    mouse = (int(canvas.width * .63), int(canvas.height * .39))
    screen = dict(x=canvas.x + mouse[0], y=canvas.y + mouse[1])
    def view():
        return (space.image.as_pointer(), tuple(space.zoom),
                tuple(canvas.view2d.region_to_view(*mouse)), tuple(canvas.view2d.region_to_view(1, 1)))
    with override(viewer_area):
        bpy.ops.image.view_zoom_ratio(ratio=2)
    yield
    before_pan = view()
    with override(viewer_area):
        bpy.ops.image.view_pan(offset=(115, -73))
    yield
    before = view()
    print("PICKER VIEW before pan / panned", before_pan, before, flush=True)
    assert before_pan[2] != before[2] and before[1][0] >= 2
    assert 0 < before[2][0] < 1 and 0 < before[2][1] < 1
    wb = 'apple_white_balance' if extension.catalog.uses_apple_input(developer) else 'white_balance'
    def sample_state(recipe):
        return recipe.neutral_x, recipe.neutral_y, getattr(recipe, wb)
    original = sample_state(developer)

    def invoke():
        with override(nodes):
            assert bpy.ops.blender_raw.pick_neutral(
                'INVOKE_DEFAULT', photo_uid=uid, tree_name=photo.tree.name,
                node_name=developer.name) == {'RUNNING_MODAL'}

    invoke()
    yield
    assert view() == before, "Entering the picker moved the view"
    window.event_simulate(type='ESC', value='PRESS', **screen)
    window.event_simulate(type='ESC', value='RELEASE', **screen)
    yield
    assert view() == before and original == sample_state(developer)
    invoke()
    yield
    assert view() == before
    window.event_simulate(type='MOUSEMOVE', value='NOTHING', **screen)
    window.event_simulate(type='LEFTMOUSE', value='PRESS', **screen)
    window.event_simulate(type='LEFTMOUSE', value='RELEASE', **screen)
    yield
    assert view() == before
    u, v = before[2]
    assert abs(developer.neutral_x - u) < 1e-6 and abs(developer.neutral_y - (1-v)) < 1e-6
    assert getattr(developer, wb) == 'SAMPLE'
    assert original == sample_state(other)
    assert original == sample_state(photo)
    deadline = time.monotonic() + 90
    def variant_ready():
        variant = developer.node_tree.nodes.get("Developed RAW")
        return variant is not None and variant.image is not None and not developer.get("recipe_dirty")
    while not variant_ready():
        assert not developer.error, developer.error
        assert time.monotonic() < deadline, "Sampled white balance did not finish"
        yield
    yield
    assert view() == before, "Redeveloping the image moved the view"
    print("PICKER VIEW final", view(), "RAW coordinate", (developer.neutral_x,developer.neutral_y), flush=True)
    extension.unregister()


steps = run()


def advance():
    try:
        next(steps)
        return .2
    except StopIteration:
        print("Zoomed and panned picker regression passed", flush=True)
    except Exception:
        traceback.print_exc()
        sys.stderr.flush()
        os._exit(1)
    bpy.ops.wm.quit_blender()


bpy.app.timers.register(advance, first_interval=1)
