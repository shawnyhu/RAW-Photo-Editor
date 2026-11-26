# SPDX-License-Identifier: GPL-3.0-or-later
"""Drive the installed operator in a Blender window and check its persisted controls."""
import os
from pathlib import Path
import sys
import time
import traceback
from tempfile import TemporaryDirectory

import bpy
import numpy as np
from bl_ext.user_default.raw_photo_editor import crop_editor, crop_node, native_effects

scratch = TemporaryDirectory()
marker = Path(sys.argv[-1])


def run():
    window = bpy.context.window_manager.windows[0]
    scene = window.scene
    bpy.context.preferences.view.show_splash = False
    window.event_simulate(type='ESC',value='PRESS')
    window.event_simulate(type='ESC',value='RELEASE')
    yield
    area = max((a for a in window.screen.areas if a.type=='VIEW_3D'),key=lambda a:a.width*a.height)
    area.type='IMAGE_EDITOR'
    canvas=next(r for r in area.regions if r.type=='WINDOW')
    prop=next(a for a in window.screen.areas if a.type=='PROPERTIES')
    controls=next(r for r in prop.regions if r.type=='WINDOW')
    image=bpy.data.images.new('Window test source',width=320,height=200)
    tree=bpy.data.node_groups.new('Source','CompositorNodeTree')
    tree.interface.new_socket(name='Image',in_out='OUTPUT',socket_type='NodeSocketColor')
    source=tree.nodes.new('CompositorNodeImage')
    source.image=image
    tree.links.new(source.outputs[0],tree.nodes.new('NodeGroupOutput').inputs[0])
    scene.compositor_effects.new('Source').node_group=tree
    effect=native_effects.create_effect(scene,crop_node.TYPE)
    node=native_effects.effect_node(effect)
    previous=area.spaces.active.image
    def invoke(kind):
        with bpy.context.temp_override(window=window,area=prop,region=controls):
            return bpy.ops.blender_raw.edit_crop('INVOKE_DEFAULT',tree_name=node.id_data.name,
                                                node_name=node.name,action=kind)
    def location(uv):
        x,y=canvas.view2d.view_to_region(*uv,clip=False)
        return dict(x=x+canvas.x,y=y+canvas.y)
    def event(kind,value,pt):
        window.event_simulate(type=kind,value=value,**location(pt))
    def offset_uv(uv,dx,dy):
        x,y=canvas.view2d.view_to_region(*uv,clip=False)
        return np.array(canvas.view2d.region_to_view(x+dx,y+dy))
    def drag(start,end,release_only=False):
        event('MOUSEMOVE','NOTHING',start)
        event('LEFTMOUSE','PRESS',start)
        yield
        assert crop_editor._active.drag is not None,'Visible handle did not accept the press'
        if not release_only:
            event('MOUSEMOVE','NOTHING',end)
            yield
        event('LEFTMOUSE','RELEASE',end)
        yield
    def finish(key):
        event(key,'PRESS',(.5,.5))
        event(key,'RELEASE',(.5,.5))
        yield
        assert crop_editor._active is None
        assert area.spaces.active.image==previous

    assert invoke('GUIDES')=={'RUNNING_MODAL'}
    yield
    assert area.spaces.active.image==image
    for pt in np.reshape(crop_editor._active.p['guides'],(4,2)):
        x,y=canvas.view2d.view_to_region(*pt,clip=False)
        assert 0<x<canvas.width and 0<y<canvas.height,'Guide was outside the fitted reference'
    # Reproduce the lower-left handle moving straight upward. The other three
    # corners stay put and the node is changed only when the editor is accepted.
    untouched=tuple(node.guides)
    yield from drag((.15,.15),(.15,.78))
    np.testing.assert_allclose(crop_editor._active.p['guides'][:2],(.15,.78),atol=.012)
    np.testing.assert_allclose(crop_editor._active.p['guides'][2:],untouched[2:],atol=1e-7)
    assert tuple(node.guides)==untouched
    yield from finish('RET')
    np.testing.assert_allclose(node.guides[:2],(.15,.78),atol=.012)
    crop_node.reset(node,bpy.context)
    assert invoke('GUIDES')=={'RUNNING_MODAL'}
    yield
    yield from drag((.85,.85),(.40,.72))
    yield from finish('RET')
    np.testing.assert_allclose(node.guides[4:6],(.40,.72),atol=.012)
    crop_node.reset(node,bpy.context)
    assert invoke('GUIDES')=={'RUNNING_MODAL'}
    yield
    yield from drag((.15,.15),(.15,.33))
    # Grabbing the edge of the handle must keep that grab offset. A release at
    # a new position must also apply even when no final MOUSEMOVE was delivered.
    start=offset_uv((.15,.33),9,7)
    yield from drag(start,start+(.04,.035),release_only=True)
    yield from finish('RET')
    assert node.mode=='MANUAL'
    np.testing.assert_allclose(node.guides[:2],(.19,.365),atol=.012)
    old=tuple(node.guides)
    assert invoke('GUIDES')=={'RUNNING_MODAL'}
    yield
    yield from drag(old[:2],(.30,.39))
    yield from finish('ESC')
    assert tuple(node.guides)==old

    # Direction selection is pending until an operation succeeds. Blank-image
    # detection and manual cancellation preserve all previously committed data.
    node.bounded_guides=False  # Simulate a saved legacy perspective result.
    before=crop_node.parameters(node)
    node.edit_direction='VERTICAL'
    assert invoke('AUTO')=={'CANCELLED'}
    assert crop_node.parameters(node)==before
    assert area.spaces.active.image==previous and crop_editor._active is None
    assert invoke('GUIDES')=={'RUNNING_MODAL'}
    yield
    assert crop_editor._active.p['direction']=='VERTICAL'
    yield from drag(old[:2],(.23,.40))
    yield from finish('ESC')
    assert crop_node.parameters(node)==before
    assert invoke('GUIDES')=={'RUNNING_MODAL'}
    yield
    yield from finish('RET')
    assert node.direction=='VERTICAL' and node.bounded_guides

    node.edit_direction='HORIZONTAL'
    assert invoke('GUIDES')=={'RUNNING_MODAL'}
    yield
    assert crop_editor._active.p['direction']=='HORIZONTAL'
    start=np.array(crop_editor._active.p['guides'][:2])
    yield from drag(start,start+(0,.04))
    yield from finish('RET')
    assert node.direction=='HORIZONTAL'
    node.edit_direction='BOTH'
    assert invoke('GUIDES')=={'RUNNING_MODAL'}
    yield
    yield from finish('RET')
    assert node.direction=='BOTH'

    # A photograph with only one usable line family can be corrected on that
    # axis. The same input must not silently downgrade a requested Both solve.
    def line_chart(direction):
        w,h=image.size
        y,x=np.indices((h,w),dtype=np.float32)
        x,y=(x+.5)/w,(y+.5)/h
        across,along=(x,y) if direction=='VERTICAL' else (y,x)
        gray=(.08+.7*np.exp(-((across-(.23+.10*along))/.008)**2)
              +.7*np.exp(-((across-(.81-.07*along))/.008)**2))
        rgba=np.stack((gray,gray,gray,np.ones_like(gray)),axis=-1)
        image.pixels.foreach_set(rgba.ravel())
        image.update()
    line_chart('VERTICAL')
    before=crop_node.parameters(node)
    assert invoke('AUTO')=={'CANCELLED'}
    assert crop_node.parameters(node)==before
    for direction in ('VERTICAL','HORIZONTAL'):
        line_chart(direction)
        node.edit_direction=direction
        assert invoke('AUTO')=={'FINISHED'}
        assert node.mode=='AUTO' and node.direction==direction
        assert area.spaces.active.image==previous and crop_editor._active is None

    assert invoke('OFF')=={'FINISHED'}
    node.preset='SQUARE'
    node.crop_size=.8
    assert invoke('CROP')=={'RUNNING_MODAL'}
    yield
    f=crop_editor._active.frame
    corners=crop_node.geom.project(f.inverse,crop_node.geom.UNIT)
    yield from drag(corners[2],corners[2]-(.08,.05))
    yield from drag((.50,.50),(.54,.47))
    event('RIGHT_BRACKET','PRESS',(.5,.5))
    event('RIGHT_BRACKET','RELEASE',(.5,.5))
    yield
    yield from finish('RET')
    assert .1<node.crop_size<.8,(node.crop_size,node.center[:])
    assert abs(node.angle-np.radians(1))<1e-5
    assert tuple(node.center)!=(.5,.5)
    assert invoke('FIT')=={'FINISHED'} and abs(node.crop_size-1)<1e-6
    print('PASS upward lower-left drag, grab offset, release position, independent guide directions, weak detection preservation, commit/cancel, resize/move frame, keyboard rotation, fit and editor restoration',flush=True)


steps=run()
deadline=time.monotonic()+60


def tick():
    try:
        assert time.monotonic()<deadline,'Timed out waiting for window'
        next(steps)
        return .22
    except StopIteration:
        marker.write_text('passed',encoding='utf8')
        bpy.ops.wm.quit_blender()
    except BaseException:
        traceback.print_exc()
        sys.stderr.flush()
        os._exit(1)


bpy.app.timers.register(tick,first_interval=1.)
