"""Real Blender window. Pass -- PHOTO.NEF with isolated profile, event simulation and decoder wheels."""
from contextlib import closing
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback

import bpy
import numpy as np
import OpenImageIO as oiio

installed = 'bl_ext.user_default.raw_photo_editor' in bpy.context.preferences.addons
if installed:
    from bl_ext.user_default import raw_photo_editor as addon
    from bl_ext.user_default.raw_photo_editor import metering, worker
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    if os.environ.get('BLENDERRAW_TEST_WHEEL_SITE'):
        sys.path.append(os.environ['BLENDERRAW_TEST_WHEEL_SITE'])
    import blender_raw as addon
    from blender_raw import metering, worker

source = Path(sys.argv[-1]).resolve()


def run():
    if not installed:
        addon.register()
    window = bpy.context.window_manager.windows[0]
    bpy.context.preferences.view.show_splash = False
    window.event_simulate(type='ESC', value='PRESS')
    window.event_simulate(type='ESC', value='RELEASE')
    yield
    with bpy.context.temp_override(window=window, area=window.screen.areas[0]):
        assert bpy.ops.blender_raw.open_workspace() == {'FINISHED'}
    while not window.workspace.get('blender_raw_workspace'):
        yield
    photo, _ = addon.catalog.add_photo(bpy.context, source)
    photo.selected = True
    assert bpy.ops.blender_raw.open_photo(uid=photo.uid) == {'FINISHED'}
    tree = photo.tree
    node = next(n for n in tree.nodes if n.bl_idname == addon.compositor.NODE_TYPE)
    meter_ui = addon.compositor.spot_meter

    def readout():
        return meter_ui._exposure_readout(node, photo)

    assert readout() == 'auto exposure off'
    node.meter_mode = 'GLOBAL'
    assert addon.jobs.developing(photo.uid) and readout() == 'Auto exposure: awaiting image'
    viewer = next(a for a in window.screen.areas if a.type == 'IMAGE_EDITOR')
    editor = next(a for a in window.screen.areas if a.ui_type == 'CompositorNodeTree')
    rows = {}
    draw_controls = meter_ui.draw_controls

    def record_controls(layout, owner, src, **kwargs):
        draw_controls(layout, owner, src, **kwargs)
        if owner == node:
            # Introspect the real RNA layout on the node and in the Node sidebar.
            column = layout.introspect()[0]['items'][-1]
            controls = column.get('items', [])
            texts = [item.get('draw_string') for item in controls]
            rows[bpy.context.region.type] = next((text for text in texts if text and 'Auto exposure:' in text), None)

    meter_ui.draw_controls = record_controls
    tree.nodes.active = node
    node.select = True
    editor.spaces.active.show_region_ui = True

    def region(area):
        return next(r for r in area.regions if r.type == 'WINDOW')

    def verify_draw(expected):
        rows.clear()
        editor.tag_redraw()
        until = time.monotonic() + 8
        while not all(kind in rows and rows[kind] == expected for kind in ('WINDOW', 'UI')):
            assert time.monotonic() < until, (expected, rows)
            yield

    while not (not addon.jobs.developing(photo.uid) and not node.get('recipe_dirty') and
               addon.compositor._meter_image(node) and viewer.spaces.active.image and viewer.spaces.active.image.has_data and
               tuple(viewer.spaces.active.image.size) == tuple(addon.compositor._meter_image(node).size)):
        assert not (node.error or photo.error), (node.error, photo.error)
        yield
    next(r for r in editor.regions if r.type == 'UI').active_panel_category = 'Node'
    with bpy.context.temp_override(window=window, area=editor, region=region(editor)):
        bpy.ops.node.view_all()
    for mode in ('GLOBAL', 'CENTER', 'HIGHLIGHTS', 'SHADOWS'):
        node.meter_mode = mode
        automatic = metering.exposure_ev(mode, addon.compositor._meter_image(node))
        expected = f'Auto exposure: {round(automatic, 2) or 0.0:+.2f} EV'
        assert readout() == expected, (mode, readout(), expected)
        yield from verify_draw(expected)
        node.exposure = 1.25
        assert readout() == expected, (mode, readout(), expected)
        node.exposure = 0
        print('METER', mode, expected, flush=True)
    # The old successful image remains bound while a new development is queued.
    addon.jobs.preview_later(photo)
    assert addon.compositor._meter_image(node) and readout() == 'Auto exposure: awaiting image'
    with bpy.context.temp_override(window=window, area=editor, region=region(editor)):
        editor.tag_redraw()
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
    assert rows == dict.fromkeys(('WINDOW', 'UI'), 'Auto exposure: awaiting image'), rows
    while addon.jobs.developing(photo.uid):
        yield
    node.as_shot = True
    assert readout() == 'auto exposure off'
    yield from verify_draw(None)
    node.auto_exposure = 'FILM_MEDIAN'
    assert node.meter_mode == 'FILM_MEDIAN'
    node.meter_mode = 'SPOT'
    assert node.auto_exposure == 'SPOT' and len(metering.mode_items(node, None)) == 5
    node.spot_radius = 5
    with bpy.context.temp_override(window=window, area=viewer, region=region(viewer)):
        bpy.ops.image.view_zoom_ratio(ratio=2)
    yield
    with bpy.context.temp_override(window=window, area=viewer, region=region(viewer)):
        bpy.ops.image.view_pan(offset=(94, -45))
    yield
    canvas = region(viewer)
    xy = (canvas.width//3, canvas.height//3)
    u,v = canvas.view2d.region_to_view(*xy)
    img = viewer.spaces.active.image
    xy_abs = dict(x=xy[0]+canvas.x, y=xy[1]+canvas.y)
    assert 0 < u < 1 and 0 < v < 1
    saved = tuple(node.spot_center)
    def invoke():
        with bpy.context.temp_override(window=window, area=editor, region=region(editor)):
            assert bpy.ops.blender_raw.pick_meter('INVOKE_DEFAULT', tree_name=tree.name, node_name=node.name) == {'RUNNING_MODAL'}
    invoke()
    yield
    window.event_simulate(type='MOUSEMOVE', value='NOTHING', **xy_abs)
    window.event_simulate(type='ESC', value='PRESS', **xy_abs)
    window.event_simulate(type='ESC', value='RELEASE', **xy_abs)
    yield
    assert tuple(node.spot_center) == saved
    invoke()
    window.event_simulate(type='MOUSEMOVE', value='NOTHING', **xy_abs)
    window.event_simulate(type='LEFTMOUSE', value='PRESS', **xy_abs)
    window.event_simulate(type='LEFTMOUSE', value='RELEASE', **xy_abs)
    yield
    np.testing.assert_allclose(node.spot_center, [u,1-v], atol=1e-6)
    assert viewer.spaces.active.image == img and canvas.view2d.region_to_view(*xy) == (u,v)
    ev_input = node.node_tree.nodes['Exposure'].inputs['Exposure']
    ev = ev_input.default_value
    assert readout() == f'Auto exposure: {round(ev, 2) or 0.0:+.2f} EV'
    yield from verify_draw(readout())
    node.spot_radius = 22
    assert abs(ev_input.default_value - ev) > .001
    node.spot_radius = 5
    assert abs(ev_input.default_value - ev) < 1e-6
    node.exposure = .5
    assert abs(ev_input.default_value - ev - .5) < 1e-5
    assert readout() == f'Auto exposure: {round(ev, 2) or 0.0:+.2f} EV'
    yield from verify_draw(readout())
    settings = window.scene.render.image_settings
    settings.file_format, settings.color_depth = 'OPEN_EXR', '32'
    with tempfile.TemporaryDirectory() as temp:
        assert bpy.ops.blender_raw.export_photos(directory=temp) == {'FINISHED'}
        while addon.jobs.export_running(): yield
        assert not addon.jobs.export_details(), addon.jobs.export_details()
        full = addon.catalog.request(photo, 'full', recipe=node)
        rawpath = Path(temp) / 'reference.exr'
        worker.process_image(dict(full, output=str(rawpath)))
        baseline = bpy.data.images.load(str(rawpath))
        full_ev = metering.exposure_ev('SPOT', baseline, spot=(*node.spot_center, node.spot_radius))
        output = next(p for p in Path(temp).glob('*.exr') if p != rawpath)
        with closing(oiio.ImageInput.open(str(output))) as out, closing(oiio.ImageInput.open(str(rawpath))) as ref:
            def patch(reader):
                spec = reader.spec()
                y = spec.height//2
                return reader.read_scanlines(0,0,y,y+1,0,0,3,oiio.FLOAT).reshape(-1,3)[::73]
            np.testing.assert_allclose(patch(out), patch(ref)*2**(full_ev+.5), rtol=3e-5, atol=1e-5)
        print(f'SPOT PASS {source.name}, zoomed Viewer center=({u:.4f}, {1-v:.4f}) radius=5% of short edge; '
              f'preview {ev:+.5f} EV, full {full_ev:+.5f} EV, exported manual +0.50 EV', flush=True)
        bpy.data.images.remove(baseline)
    meter_ui.draw_controls = draw_controls
    if not installed:
        addon.unregister()


steps = run()
deadline = time.monotonic() + 180

def advance():
    try:
        assert time.monotonic() < deadline, 'Spot test timed out'
        next(steps)
        return .2
    except StopIteration:
        bpy.ops.wm.quit_blender()
    except Exception:
        traceback.print_exc()
        sys.stderr.flush()
        os._exit(1)

bpy.app.timers.register(advance, first_interval=1)
