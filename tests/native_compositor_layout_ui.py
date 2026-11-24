"""Regress native Compositor positioning and effect selection in a real window.

blender --factory-startup --enable-event-simulate -p 50 50 1600 1000 -P tests/native_compositor_layout_ui.py
"""
from pathlib import Path
import sys
import traceback

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import blender_raw
from blender_raw import catalog, compositor, gallery, native_effects


def wait_for(predicate, message):
    for _ in range(100):
        if predicate():
            return
        yield
    raise AssertionError(message)


def test():
    assert not bpy.app.background, 'This regression requires real window redraws'
    blender_raw.register()
    window = bpy.context.window_manager.windows[0]
    bpy.context.preferences.view.show_splash = False
    window.event_simulate(type='ESC', value='PRESS')
    window.event_simulate(type='ESC', value='RELEASE')
    yield
    bpy.context.scene.blender_raw.owner = True
    photo = catalog.library(bpy.context).photos.add()
    photo.uid = photo.name = 'Compositor layout photo'
    catalog.set_camera_input(photo, 0)
    photo.image = bpy.data.images.new('Layout image', width=32, height=24, float_buffer=True)
    compositor.ensure_photo_scene(bpy.context, photo)
    with bpy.context.temp_override(window=window, area=window.screen.areas[0]):
        assert bpy.ops.blender_raw.new_workspace() == {'FINISHED'}
    yield from wait_for(lambda: window.workspace.get('blender_raw_workspace') and not gallery._timers,
                        'Photo Workspace layout did not finish')
    compositor.open_photo(bpy.context, photo)
    yield
    screen = window.screen
    panel = next(area for area in screen.areas if area.type == 'PROPERTIES')
    region = next(region for region in panel.regions if region.type == 'WINDOW')
    navigation = next(region for region in panel.regions if region.type == 'NAVIGATION_BAR')
    yield from wait_for(lambda: screen.get('photo_editor_navigation_default') and navigation.width <= 1,
                        'Photo Workspace Navigation Bar was not hidden by default')
    with bpy.context.temp_override(window=window, area=panel, region=region):
        assert bpy.ops.screen.region_toggle(region_type='NAVIGATION_BAR') == {'FINISHED'}
    yield
    assert navigation.width > 1
    gallery._activate_photo_workspace(window)
    yield
    yield
    assert navigation.width > 1, 'Workspace activation overwrote the user navigation preference'
    # An older Photo Workspace receives the default once when first opened.
    del screen['photo_editor_navigation_default']
    gallery._activate_photo_workspace(window)
    yield from wait_for(lambda: screen.get('photo_editor_navigation_default') and navigation.width <= 1,
                        'Existing Photo Workspace did not receive the navigation default')
    print('PASS default Navigation Bar hiding, existing workspace setup and manual preference preservation', flush=True)
    developer = window.scene.compositor_effects[1]
    custom = native_effects.create_effect(window.scene, native_effects.CUSTOM_TYPE, photo=photo)

    def toggle():
        active = window.scene.compositor_effects.active
        with bpy.context.temp_override(window=window, area=panel, region=region):
            result = bpy.ops.blender_raw.toggle_compositor()
        assert window.scene.compositor_effects.active == active, 'Global toggle changed the active effect'
        return result

    def settled(opened):
        viewer, nodes = gallery.viewer_column(screen)
        return bool(viewer and bool(nodes) == opened and not gallery._compositor_layout_timers)

    def check_below():
        viewer, nodes = gallery.viewer_column(screen)
        assert viewer and nodes and nodes.y < viewer.y, [(a.ui_type, a.y, a.height) for a in screen.areas]
        assert len(screen.areas) == 4
        assert abs(viewer.x - nodes.x) <= 1 and abs(viewer.width - nodes.width) <= 1
        return viewer, nodes

    window.scene.compositor_effects.active = developer
    for fraction in (.31, .49, .51, .72, .85):
        screen['photo_editor_node_height'] = fraction
        assert toggle() == {'FINISHED'}
        assert toggle() == {'CANCELLED'}  # No second split before the first redraw.
        yield from wait_for(lambda: settled(True), f'Opening {fraction:.0%} pane did not settle')
        viewer, nodes = check_below()
        actual = nodes.height / (viewer.height + nodes.height)
        assert abs(actual - fraction) < .02, (fraction, actual)
        yield from wait_for(lambda: nodes.spaces.active.node_tree == developer.node_group, 'Wrong effect tree after opening')
        assert toggle() == {'FINISHED'}
        yield from wait_for(lambda: settled(False), 'Closing compositor did not settle')
        assert len(screen.areas) == 3
    print('PASS compositor stays below Viewer at 31%, 49%, 51%, 72% and 85%', flush=True)

    screen['photo_editor_node_height'] = .31
    assert toggle() == {'FINISHED'}
    yield from wait_for(lambda: settled(True), 'Compositor did not open before resize')
    viewer, nodes = check_below()
    edge = (viewer.x + viewer.width // 2, viewer.y)
    delta = round((viewer.height + nodes.height) * .73) - nodes.height
    window.event_simulate(type='MOUSEMOVE', value='NOTHING', x=edge[0], y=edge[1])
    yield
    with bpy.context.temp_override(window=window, area=viewer):
        assert bpy.ops.screen.area_move(x=edge[0], y=edge[1], delta=delta) == {'FINISHED'}
    yield
    viewer, nodes = check_below()
    assert nodes.height > viewer.height
    resized_height = nodes.height
    for _ in range(3):
        assert toggle() == {'FINISHED'}
        yield from wait_for(lambda: settled(False), 'Resized pane did not close')
        assert toggle() == {'FINISHED'}
        yield from wait_for(lambda: settled(True), 'Resized pane did not reopen')
        viewer, nodes = check_below()
        assert abs(nodes.height - resized_height) <= 3, (resized_height, nodes.height)
    print('PASS user-resized pane above half-height preserves position and height over three toggles', flush=True)

    pointer, height = nodes.as_pointer(), nodes.height
    window.scene.compositor_effects.active = custom
    yield from wait_for(lambda: nodes.spaces.active.node_tree == custom.node_group,
                        'Native active effect did not update the open Compositor')
    viewer, nodes = check_below()
    assert nodes.as_pointer() == pointer and nodes.height == height
    assert window.scene.compositor_effects.active == custom
    assert nodes.spaces.active.node_tree == custom.node_group
    assert toggle() == {'FINISHED'}
    yield from wait_for(lambda: settled(False), 'Global toggle did not close the pane')
    assert window.scene.compositor_effects.active == custom
    print('PASS native effect selection reuses the pane; global toggle preserves the selected effect', flush=True)

    # Reproduce a layout saved after the previous bug: Compositor above Viewer.
    screen['photo_editor_node_height'] = .31
    window.scene.compositor_effects.active = developer
    assert toggle() == {'FINISHED'}
    yield from wait_for(lambda: settled(True), 'Pane did not open before inverted-layout repair')
    viewer, nodes = check_below()
    image = viewer.spaces.active.image
    viewer.type = 'NODE_EDITOR'
    viewer.ui_type = 'CompositorNodeTree'
    viewer.spaces.active.pin = False
    viewer.spaces.active.path.start(developer.node_group)
    nodes.type = 'IMAGE_EDITOR'
    nodes.spaces.active.image = image
    yield
    lower_viewer, upper_nodes = gallery.viewer_column(screen)
    assert upper_nodes.y > lower_viewer.y
    inverted_height = upper_nodes.height
    assert toggle() == {'FINISHED'}
    yield from wait_for(lambda: settled(False), 'Old inverted layout did not close')
    window.scene.compositor_effects.active = custom
    assert toggle() == {'FINISHED'}
    yield from wait_for(lambda: settled(True) and gallery.viewer_column(screen)[1].y < gallery.viewer_column(screen)[0].y,
                        'Old inverted layout was not repaired')
    viewer, nodes = check_below()
    assert abs(nodes.height - inverted_height) <= 3
    assert nodes.spaces.active.node_tree == custom.node_group
    print('NATIVE_COMPOSITOR_LAYOUT_UI_PASS: lower-pane placement, user height, effect selection and inverted-layout repair', flush=True)


steps = test()


def tick():
    try:
        next(steps)
        return .12
    except StopIteration:
        bpy.ops.wm.quit_blender()
    except BaseException:
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(tick, first_interval=1.0)
