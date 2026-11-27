# SPDX-License-Identifier: GPL-3.0-or-later
"""Check real canvas pixels, image preservation and workspace-local persistence.

blender --factory-startup --enable-event-simulate -p 30 30 1200 850 \
  --python-exit-code 1 --python tests/viewer_background_ui.py -- /path/to/output
"""
import hashlib
import os
from pathlib import Path
import sys
import time
import traceback

import bpy
import gpu
import numpy as np

if '--installed' in sys.argv:
    from bl_ext.user_default import raw_photo_editor as blender_raw
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import blender_raw
compositor = blender_raw.compositor
gallery = blender_raw.gallery
viewer_background = blender_raw.viewer_background

folder = Path(sys.argv[sys.argv.index('--')+1]).resolve()
folder.mkdir(parents=True, exist_ok=True)
captures = {}
drawn_panels = []
handles = []
capture_requested = False


def capture():
    context = bpy.context
    if not capture_requested or context.area.type not in {'IMAGE_EDITOR','NODE_EDITOR'} or context.region.type != 'WINDOW':
        return
    w,h=context.region.width,context.region.height
    buffer=gpu.state.active_framebuffer_get().read_color(0,0,w,h,4,0,'FLOAT')
    captures[context.area.as_pointer()]=np.array(buffer,dtype=np.float32).reshape(h,w,4)


def panel_draw(self, context):
    drawn_panels.append(context.workspace.name)


def settle(area):
    global capture_requested
    capture_requested=True
    captures.clear()
    area.tag_redraw()
    for _ in range(12):
        yield
        if area.as_pointer() in captures:
            break
    assert area.as_pointer() in captures,'Viewer did not draw'
    capture_requested=False


def image_pixels(image):
    pixels=np.empty(len(image.pixels),dtype=np.float32)
    image.pixels.foreach_get(pixels)
    return pixels


def rgba_image(name, width, height):
    image=bpy.data.images.new(name,width=width,height=height,alpha=True,float_buffer=True)
    y,x=np.indices((height,width),dtype=np.float32)
    rgba=np.stack((.2+.6*x/width,.1+.4*y/height,np.full_like(x,.15),
                   np.where(x<width/2,1.,.35)),axis=-1)
    image.pixels.foreach_set(rgba.astype(np.float32).ravel())
    image.update()
    return image


def region(area):
    return next(r for r in area.regions if r.type=='WINDOW')


def interior_samples(area, pixels):
    view=region(area).view2d
    points=[view.view_to_region(u,v,clip=False) for u,v in ((.2,.3),(.7,.3),(.3,.7),(.75,.7))]
    return np.array([pixels[y,x,:3] for x,y in points if 3<x<pixels.shape[1]-3 and 3<y<pixels.shape[0]-3])


def assert_canvas(area, pixels, color):
    canvas=region(area)
    bounds=viewer_background.image_bounds(canvas) if area.spaces.active.image else None
    samples=[]
    for x0,y0,x1,y1 in viewer_background.outside_rectangles(canvas.width,canvas.height,bounds):
        if x1-x0>12 and y1-y0>12:
            # Stay clear of the native scrollbar and overlapping sidebar.
            x,y=round((x0+x1)/2),round((y0+y1)/2)
            if any(r.type in {'UI','TOOLS'} and r.width>2
                   and r.x<=canvas.x+x<r.x+r.width and r.y<=canvas.y+y<r.y+r.height
                   for r in area.regions):
                continue
            samples.append(pixels[y,x,:3])
    assert samples,'No visible background sample remained'
    np.testing.assert_allclose(samples,np.tile(color[:3],(len(samples),1)),atol=.012,rtol=0)


def test():
    assert not bpy.app.background
    if '--installed' not in sys.argv:
        blender_raw.register()
    window=bpy.context.window_manager.windows[0]
    window.scene.blender_raw.owner=True
    bpy.context.preferences.view.show_splash=False
    window.event_simulate(type='ESC',value='PRESS')
    window.event_simulate(type='ESC',value='RELEASE')
    yield
    area=max((a for a in window.screen.areas if a.type=='VIEW_3D'),key=lambda a:a.width*a.height)
    area.type='IMAGE_EDITOR'
    area.spaces.active.mode='VIEW'
    area.spaces.active.show_region_ui=True
    image=rgba_image('Viewer background source',320,180)
    other=rgba_image('Other photograph',180,320)
    source_pixels=image_pixels(image)
    area.spaces.active.image=image
    yield
    canvas=region(area)
    with bpy.context.temp_override(window=window,area=area,region=canvas):
        bpy.ops.image.view_zoom_ratio(ratio=1.)
    handles.append((bpy.types.SpaceImageEditor,
                    bpy.types.SpaceImageEditor.draw_handler_add(capture,(),'WINDOW','POST_PIXEL')))
    handles.append((bpy.types.SpaceNodeEditor,
                    bpy.types.SpaceNodeEditor.draw_handler_add(capture,(),'WINDOW','POST_PIXEL')))
    gallery_area=next(a for a in window.screen.areas if a.type=='OUTLINER')
    gallery_area.ui_type=gallery.GALLERY_TYPE
    yield from settle(gallery_area)
    gallery_pixels=captures[gallery_area.as_pointer()][15:30,15:30,:3].copy()
    viewer_background.BR_PT_viewer_background.append(panel_draw)
    workspace=window.workspace
    original_theme=tuple(bpy.context.preferences.themes[0].image_editor.space.back)
    yield from settle(area)
    native=captures[area.as_pointer()].copy()
    native_inside=interior_samples(area,native)
    assert len(native_inside)==4
    image.save_render(str(folder/'before.png'),scene=window.scene)
    initial_export=hashlib.sha256((folder/'before.png').read_bytes()).hexdigest()

    workspace['blender_raw_workspace']=True
    assert workspace.photo_editor_canvas_background=='BLACK'
    sidebar=next(r for r in area.regions if r.type=='UI')
    for _ in range(10):
        try:
            sidebar.active_panel_category='Editor'
            break
        except TypeError:
            yield
    for setting,color in viewer_background.COLORS.items():
        workspace.photo_editor_canvas_background=setting
        yield from settle(area)
        pixels=captures[area.as_pointer()]
        assert_canvas(area,pixels,color)
        np.testing.assert_allclose(interior_samples(area,pixels),native_inside,atol=1e-5)
        np.testing.assert_array_equal(image_pixels(image),source_pixels)
        image.save_render(str(folder/f'{setting.lower()}.png'),scene=window.scene)
        assert hashlib.sha256((folder/f'{setting.lower()}.png').read_bytes()).hexdigest()==initial_export
    assert drawn_panels and sidebar.active_panel_category=='Editor','Editor card was not drawn'
    assert tuple(bpy.context.preferences.themes[0].image_editor.space.back)==original_theme
    yield from settle(gallery_area)
    np.testing.assert_array_equal(captures[gallery_area.as_pointer()][15:30,15:30,:3],gallery_pixels)

    # Native view operators move the image without leaving old-color strips or
    # drawing over the visible portion of a photograph larger than the editor.
    for zoom,offset in ((.65,(57.,-39.)),(3.5,(-310.,85.)),(.5,(900.,0.))):
        with bpy.context.temp_override(window=window,area=area,region=canvas):
            bpy.ops.image.view_zoom_ratio(ratio=zoom)
            bpy.ops.image.view_pan(offset=offset)
        yield from settle(area)
        assert_canvas(area,captures[area.as_pointer()],viewer_background.COLORS['LIGHT'])
        np.testing.assert_array_equal(image_pixels(image),source_pixels)

    # Every photograph scene shares the workspace setting, while ordinary
    # Image Editors outside that workspace retain the native background.
    second_scene=bpy.data.scenes.new('Viewer background second scene')
    window.scene=second_scene
    second_scene.render.resolution_x,second_scene.render.resolution_y=other.size
    second_scene.render.resolution_percentage=100
    second_scene.render.compositor_device='CPU'
    tree=bpy.data.node_groups.new('Viewer background source effect','CompositorNodeTree')
    tree.interface.new_socket(name='Image',in_out='OUTPUT',socket_type='NodeSocketColor')
    source=tree.nodes.new('CompositorNodeImage')
    source.image=other
    tree.links.new(source.outputs['Image'],tree.nodes.new('NodeGroupOutput').inputs[0])
    tree.links.new(source.outputs['Image'],tree.nodes.new('CompositorNodeViewer').inputs['Image'])
    effect=second_scene.compositor_effects.new('Background test photo')
    effect.node_group=tree
    second_scene.compositor_effects.active=effect
    viewer=compositor.viewer_image()
    area.spaces.active.image=viewer
    for _ in range(80):
        area.tag_redraw()
        yield
        if viewer.has_data and tuple(viewer.size)==tuple(other.size) and not bpy.app.is_job_running('COMPOSITE'):
            break
    assert viewer.has_data and tuple(viewer.size)==tuple(other.size),'Actual Compositing Viewer did not evaluate'
    with bpy.context.temp_override(window=window,area=area,region=canvas):
        bpy.ops.image.view_all(fit_view=True)
        bpy.ops.image.view_zoom_ratio(ratio=.65)
    yield from settle(area)
    assert workspace.photo_editor_canvas_background=='LIGHT'
    assert_canvas(area,captures[area.as_pointer()],viewer_background.COLORS['LIGHT'])
    viewer_pixels=image_pixels(viewer)
    viewer_inside=interior_samples(area,captures[area.as_pointer()])
    viewer.save_render(str(folder/'viewer-before.png'),scene=second_scene)
    viewer_export=hashlib.sha256((folder/'viewer-before.png').read_bytes()).hexdigest()
    workspace.photo_editor_canvas_background='BLACK'
    yield from settle(area)
    assert_canvas(area,captures[area.as_pointer()],viewer_background.COLORS['BLACK'])
    np.testing.assert_array_equal(image_pixels(viewer),viewer_pixels)
    np.testing.assert_allclose(interior_samples(area,captures[area.as_pointer()]),viewer_inside,atol=1e-5)
    viewer.save_render(str(folder/'viewer-after.png'),scene=second_scene)
    assert hashlib.sha256((folder/'viewer-after.png').read_bytes()).hexdigest()==viewer_export
    workspace.photo_editor_canvas_background='LIGHT'
    area.spaces.active.image=None
    yield from settle(area)
    assert_canvas(area,captures[area.as_pointer()],viewer_background.COLORS['LIGHT'])
    area.spaces.active.image=image
    with bpy.context.temp_override(window=window,area=area,region=canvas):
        bpy.ops.image.view_zoom_ratio(ratio=1.)
        bpy.ops.image.view_all(fit_view=True)
    workspace.photo_editor_canvas_background='DARK'
    bpy.ops.wm.save_as_mainfile(filepath=str(folder/'viewer-background.blend'))
    assert workspace.photo_editor_canvas_background=='DARK'
    yield from settle(area)
    bpy.ops.screen.screenshot(filepath=str(folder/'viewer-background.png'))
    workspace['blender_raw_workspace']=False
    yield from settle(area)
    ordinary=captures[area.as_pointer()]
    assert not np.allclose(ordinary[20,20,:3],viewer_background.COLORS['DARK'][:3],atol=.012)
    assert tuple(bpy.context.preferences.themes[0].image_editor.space.back)==original_theme
    assert not viewer_background.BR_PT_viewer_background.poll(bpy.context)
    print('PASS Viewer canvas: three real colors, unchanged image/alpha/export, pan/zoom/clipping, Editor card, scene/image switching, workspace save and untouched theme',flush=True)

    # A fresh Photo Workspace starts with Grid off. An existing workspace
    # carries its own native overlay settings when reopened.
    area.spaces.active.overlay.show_grid_background = True
    with bpy.context.temp_override(window=window, area=area):
        assert bpy.ops.blender_raw.new_workspace() == {'FINISHED'}
    while gallery._timers:
        yield
    assert window.workspace != workspace and window.workspace.get('blender_raw_workspace')
    viewer_area, _ = gallery.viewer_column(window.screen)
    assert not viewer_area.spaces.active.overlay.show_grid_background
    assert area.spaces.active.overlay.show_grid_background
    viewer_area.spaces.active.overlay.show_grid_background = True
    with bpy.context.temp_override(window=window, area=viewer_area):
        assert bpy.ops.blender_raw.open_workspace() == {'FINISHED'}
    while gallery._timers:
        yield
    assert viewer_area.spaces.active.overlay.show_grid_background
    project = str(folder / 'viewer-grid.blend')
    assert bpy.ops.wm.save_as_mainfile(filepath=project) == {'FINISHED'}
    assert bpy.ops.wm.open_mainfile(filepath=project, load_ui=True) == {'FINISHED'}
    viewer_area, _ = gallery.viewer_column(bpy.context.window_manager.windows[0].screen)
    assert viewer_area.spaces.active.overlay.show_grid_background
    print('PASS Viewer grid: off by default, ordinary editor untouched, manual choice survives workspace and file reopen', flush=True)


steps=test()
deadline=time.monotonic()+90


def finish():
    for space,handle in handles:
        space.draw_handler_remove(handle,'WINDOW')
    viewer_background.BR_PT_viewer_background.remove(panel_draw)


def tick():
    try:
        assert time.monotonic()<deadline,'Timed out waiting for Viewer redraw'
        next(steps)
        return .2
    except StopIteration:
        finish()
        (folder/'passed').write_text('passed',encoding='utf8')
        bpy.ops.wm.quit_blender()
    except BaseException:
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)


bpy.app.timers.register(tick,first_interval=1.)
