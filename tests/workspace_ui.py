"""Check the default photo workspace in a real Blender 5.3 window.

blender --factory-startup --enable-event-simulate -p 50 50 1600 960 --python tests/workspace_ui.py
"""
import os
from pathlib import Path
import sys
import tempfile
import traceback

import bpy

installed = "bl_ext.user_default.raw_photo_editor" in bpy.context.preferences.addons
if installed:
    from bl_ext.user_default import raw_photo_editor as addon
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import blender_raw as addon


def wait_for(predicate, message):
    for _ in range(60):
        if predicate():
            return
        yield
    raise AssertionError(message)


def new_empty_project():
    # Start a new, empty file while keeping the installed extension's preferences and wheels.
    assert bpy.ops.wm.read_homefile(use_factory_startup=True, use_empty=True) == {'FINISHED'}


def draw_adjustment_cards(window, editor):
    # Exercise the real UI layout. Group panels and some built-in nodes have
    # different declarations from a simple Exposure socket.
    catalog = addon.catalog
    photo = catalog.library(bpy.context).photos.add()
    photo.uid = photo.name = "Stack UI Regression"
    tree = addon.compositor.open_photo(bpy.context, photo)
    region = next(r for r in editor.regions if r.type == 'WINDOW')
    for kind, _, _ in addon.photo_stack.ADJUSTMENTS:
        with bpy.context.temp_override(window=window, area=editor, region=region):
            assert bpy.ops.blender_raw.add_adjustment(tree_name=tree.name, kind=kind) == {'FINISHED'}
            node = tree.nodes.active
            editor.tag_redraw()
        yield
        with bpy.context.temp_override(window=window, area=editor, region=region):
            assert bpy.ops.blender_raw.edit_adjustment(tree_name=tree.name, node_name=node.name,
                                                      action='REMOVE') == {'FINISHED'}


def test():
    if not installed:
        addon.register()
    gallery = addon.gallery
    window = bpy.context.window_manager.windows[0]
    bpy.context.preferences.view.show_splash = False
    window.event_simulate(type='ESC', value='PRESS')
    window.event_simulate(type='ESC', value='RELEASE')
    yield
    initial_workspace = window.workspace
    original_scene = window.scene
    original_scene.view_settings.exposure = 0.75
    other_sizes = [(a.x, a.y, a.width, a.height) for a in window.screen.areas]
    lib = addon.catalog.library(bpy.context)
    assert not bpy.data.is_saved and not lib.pack_cache_images
    # Emulate a startup file that previously had both policies enabled.
    lib.pack_cache_images = True
    bpy.data.use_autopack = True
    with bpy.context.temp_override(window=window, area=window.screen.areas[0]):
        assert bpy.ops.blender_raw.open_workspace() == {'FINISHED'}
    assert not bpy.data.use_autopack and not lib.pack_cache_images
    yield from wait_for(lambda: window.workspace.get("blender_raw_workspace"), "Workspace creation timed out")
    assert window.workspace.name == 'RAW Photo Editor'
    photo_workspace = window.workspace
    studio = window.scene
    assert studio != original_scene and studio.get(addon.compositor.STUDIO_TAG)
    assert studio.view_settings.view_transform == "Standard" and studio.view_settings.exposure == 0
    assert photo_workspace.use_pin_scene
    stack_type = addon.stack_editor.EDITOR_TYPE
    assert len(window.screen.areas) == 3, [(a.ui_type, a.width, a.height) for a in window.screen.areas]
    left = next(a for a in window.screen.areas if a.ui_type == gallery.GALLERY_TYPE)
    top = next(a for a in window.screen.areas if a.type == 'IMAGE_EDITOR')
    editor = next(a for a in window.screen.areas if a.ui_type == stack_type)
    assert left.x < top.x < editor.x and left.y == top.y == editor.y
    assert left.height == top.height == editor.height
    assert not editor.spaces.active.show_region_ui and not editor.spaces.active.show_region_toolbar
    original_view = top.spaces.active
    def toggle():
        with bpy.context.temp_override(window=window, area=editor):
            assert bpy.ops.blender_raw.toggle_compositor() == {'FINISHED'}
    heights = []
    for _ in range(3):
        toggle()
        yield
        viewer, bottom = gallery.viewer_column(window.screen)
        assert viewer == top and bottom and top.spaces.active == original_view
        heights.append(bottom.height)
        assert max(heights) - min(heights) <= 1, heights
        assert bottom.y == left.y < top.y and len(window.screen.areas) == 4
        assert abs(bottom.height / (top.height + bottom.height) - .31) < .02
        toggle()
        yield
        assert gallery.viewer_column(window.screen) == (top, None) and len(window.screen.areas) == 3
        assert top.spaces.active == original_view and top.height == left.height
    toggle()
    yield
    _, bottom = gallery.viewer_column(window.screen)
    region = next(r for r in left.regions if r.type == 'WINDOW')
    with bpy.context.temp_override(window=window, area=left, region=region):
        grid = gallery._layout(bpy.context, [])
        assert addon.catalog.library(bpy.context).thumbnail_scale == 'M'
        assert grid.columns == 1 and grid.card_w == round(300 * grid.scale), grid
    assert top.spaces.active.image and top.spaces.active.image.type == 'COMPOSITING'
    assert top.spaces.active.image.use_view_as_render
    assert top.spaces.active.show_region_ui and bottom.spaces.active.show_region_ui
    tabs = lambda area: next(r.active_panel_category for r in area.regions if r.type == 'UI')
    yield from wait_for(lambda: tabs(top) == 'Scopes', 'Image Editor did not select Scopes')
    tree = bpy.data.node_groups.new("Workspace Test", 'CompositorNodeTree')
    tree.interface.new_socket(name='Image', in_out='OUTPUT', socket_type='NodeSocketColor')
    tree.nodes.active = tree.nodes.new('NodeGroupOutput')
    effect = window.scene.compositor_effects.new("Workspace Test")
    effect.node_group = tree
    window.scene.compositor_effects.active = effect
    bottom.tag_redraw()
    gallery.finish_workspace_panels(window)
    yield from wait_for(lambda: tabs(bottom) == 'Node', 'Compositor did not select Node')
    assert not window.workspace.get("photo_editor_pending_tabs")
    print(f"Photo workspace: {grid.columns} columns, {grid.card_w} physical pixels per thumbnail; "
          f"sidebar tabs {tabs(top)} / {tabs(bottom)}", flush=True)
    # A user can choose another tab. Reopening this customized workspace keeps it.
    ui = next(r for r in top.regions if r.type == 'UI')
    ui.active_panel_category = 'View'
    lib.pack_cache_images = True
    bpy.data.use_autopack = True
    window.workspace = initial_workspace
    yield from wait_for(lambda: window.workspace == initial_workspace, 'Workspace switch timed out')
    assert other_sizes == [(a.x, a.y, a.width, a.height) for a in window.screen.areas]
    assert window.scene == original_scene
    assert original_scene.view_settings.view_transform == "AgX" and original_scene.view_settings.exposure == 0.75
    # Direct tab switches must also migrate an existing, unpinned photo workspace.
    photo_workspace.use_pin_scene = False
    studio.display_settings.display_device = "Display P3"
    studio.view_settings.view_transform = "AgX"
    window.workspace = photo_workspace
    yield from wait_for(lambda: window.workspace == photo_workspace and window.scene == studio
                        and studio.view_settings.view_transform == "Standard", "Direct photo tab switch failed")
    assert studio.display_settings.display_device == "sRGB"
    window.workspace = initial_workspace
    yield from wait_for(lambda: window.workspace == initial_workspace, 'Return to original workspace failed')
    assert window.scene == original_scene
    assert bpy.data.use_autopack and lib.pack_cache_images
    with bpy.context.temp_override(window=window, area=window.screen.areas[0]):
        bpy.ops.blender_raw.open_workspace()
    yield from wait_for(lambda: window.workspace.name == 'RAW Photo Editor', 'Reopen failed')
    assert tabs(top) == 'View' and tabs(bottom) == 'Node'
    assert not bpy.data.use_autopack and not lib.pack_cache_images
    yield from draw_adjustment_cards(window, editor)
    original = window.workspace
    lib.pack_cache_images = True
    bpy.data.use_autopack = True
    if installed:
        prefs = bpy.context.preferences.addons[addon.__package__].preferences
        assert (prefs.thumbnail_s, prefs.thumbnail_m, prefs.thumbnail_l) == (200, 300, 400)
        with bpy.context.temp_override(window=window, area=left, region=region):
            prefs.thumbnail_m = 120
            smaller = gallery._layout(bpy.context, [])
            assert smaller.columns > 1 and smaller.card_w <= round(120 * smaller.scale)
        prefs.thumbnail_m = 340
    # Request a fresh default from a customized project, without disturbing its existing tab.
    with bpy.context.temp_override(window=window, area=top):
        assert bpy.ops.blender_raw.new_workspace() == {'FINISHED'}
    yield from wait_for(lambda: window.workspace != original and not gallery._timers, 'New workspace failed')
    assert tabs(top) == 'View'
    fresh_top = next(a for a in window.screen.areas if a.type == 'IMAGE_EDITOR')
    fresh_left = next(a for a in window.screen.areas if a.ui_type == gallery.GALLERY_TYPE)
    assert gallery.viewer_column(window.screen) == (fresh_top, None)
    assert any(a.ui_type == stack_type for a in window.screen.areas)
    yield from wait_for(lambda: tabs(fresh_top) == 'Scopes', 'Fresh Scopes tab failed')
    assert fresh_left.height == left.height
    if installed:
        fresh_region = next(r for r in fresh_left.regions if r.type == 'WINDOW')
        def custom_width_ready():
            with bpy.context.temp_override(window=window, area=fresh_left, region=fresh_region):
                grid = gallery._layout(bpy.context, [])
                return grid.columns == 1 and grid.card_w == round(340 * grid.scale)
        yield from wait_for(custom_width_ready, f"Custom 340px thumbnail failed in {fresh_left.width}px Gallery")
        prefs.property_unset("thumbnail_m")
    else:
        assert fresh_left.width == left.width
    assert not bpy.data.use_autopack and not lib.pack_cache_images
    fresh_left.ui_type = 'CompositorNodeTree'
    left.ui_type = 'CompositorNodeTree'
    with tempfile.TemporaryDirectory() as folder:
        project = str(Path(folder) / 'Workspace.blend')
        studio.view_settings.exposure = 1.5
        assert bpy.ops.wm.save_as_mainfile(filepath=project) == {'FINISHED'}
        assert bpy.ops.wm.open_mainfile(filepath=project) == {'FINISHED'}
        window = bpy.context.window_manager.windows[0]
        assert window.scene.get(addon.compositor.STUDIO_TAG)
        assert window.scene.view_settings.view_transform == "Standard" and window.scene.view_settings.exposure == 0
        with bpy.context.temp_override(window=window, area=window.screen.areas[0]):
            assert bpy.ops.blender_raw.open_workspace() == {'FINISHED'}
        assert not bpy.data.use_autopack and not addon.catalog.library(bpy.context).pack_cache_images
        yield from wait_for(lambda: not gallery._timers, 'Saved workspace reopen timed out')
    # A new unsaved project must get the default again in the same application.
    new_empty_project()
    window = bpy.context.window_manager.windows[0]
    bpy.data.use_autopack = True
    with bpy.context.temp_override(window=window, area=window.screen.areas[0]):
        assert bpy.ops.blender_raw.open_workspace() == {'FINISHED'}
    assert not bpy.data.use_autopack and not addon.catalog.library(bpy.context).pack_cache_images
    yield from wait_for(lambda: not gallery._timers, 'Blank file workspace creation timed out')
    # Explicitly opening an unsaved project with linked media also disables auto-pack.
    new_empty_project()
    window = bpy.context.window_manager.windows[0]
    image = bpy.data.images.new('Plate', 4, 4)
    image.filepath = '//plate.exr'
    image.source = 'FILE'
    bpy.data.use_autopack = True
    with bpy.context.temp_override(window=window, area=window.screen.areas[0]):
        assert bpy.ops.blender_raw.open_workspace() == {'FINISHED'}
    assert not bpy.data.use_autopack
    yield from wait_for(lambda: not gallery._timers, 'Custom file workspace creation timed out')
    for area in window.screen.areas:
        if area.ui_type == gallery.GALLERY_TYPE:
            area.ui_type = 'CompositorNodeTree'
    print("Verified packing defaults for new, reopened, and populated projects", flush=True)
    if not installed:
        addon.unregister()


steps = test()


def advance():
    try:
        next(steps)
        return .14
    except StopIteration:
        print("Photo workspace regression passed", flush=True)
    except Exception:
        traceback.print_exc()
        sys.stderr.flush()
        os._exit(1)
    bpy.ops.wm.quit_blender()


bpy.app.timers.register(advance, first_interval=1., persistent=True)
