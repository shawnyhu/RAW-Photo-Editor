# SPDX-License-Identifier: GPL-3.0-or-later
"""Actual native card, histogram drags, automatic toggle and cancel/Undo.

blender --enable-event-simulate --factory-startup --offline-mode -P tests/auto_level_ui.py
Installed copy: omit --factory-startup and append -- --installed.
"""
from pathlib import Path
import os
import sys
import time
import traceback
import runpy

import bpy
import numpy as np

if '--installed' in sys.argv:
    from bl_ext.user_default import raw_photo_editor as addon
else:
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    import blender_raw as addon
    addon.register()
from importlib import import_module
ui=import_module(addon.__name__+'.levels_ui')
levels=import_module(addon.__name__+'.levels_node')
effects=addon.native_effects


def shot(label):
    folder=Path(os.environ.get('PHOTO_UI_TEST_SCREENSHOTS',Path(__file__).resolve().parents[1]/'dist'/'auto-level-ui'))
    folder.mkdir(parents=True,exist_ok=True)
    bpy.ops.screen.screenshot(filepath=str(folder/(label+'.png')))


def event(window,kind,value,x=0,y=0):
    window.event_simulate(type=kind,value=value,x=round(x),y=round(y))
    yield
    yield


def click(window,x,y):
    yield from event(window,'MOUSEMOVE','NOTHING',x,y)
    yield from event(window,'LEFTMOUSE','PRESS',x,y)
    yield from event(window,'LEFTMOUSE','RELEASE',x,y)


def run():
    window=bpy.context.window_manager.windows[0]
    bpy.context.preferences.view.show_splash=False
    yield from event(window,'ESC','PRESS')
    yield from event(window,'ESC','RELEASE')
    with bpy.context.temp_override(window=window,area=window.screen.areas[0]):
        assert bpy.ops.blender_raw.new_workspace()=={'FINISHED'}
    while not window.screen.get('photo_editor_navigation_default') or addon.gallery._timers:
        yield
    scene=window.scene
    pixels=np.ones((96,512,4),np.float32)
    values=.12+.46*np.linspace(0,1,512,dtype=np.float32)**3
    pixels[:,:,:3]=values[None,:,None]
    pixels[10:20,10:20]=0
    image=bpy.data.images.new('Levels UI Input',width=512,height=96,alpha=True,float_buffer=True)
    image.pixels.foreach_set(pixels.ravel())
    image.pack()
    image.colorspace_settings.name='Linear Rec.709'
    image.alpha_mode='PREMUL'
    source=effects.create_effect(scene,effects.CUSTOM_TYPE)
    group=source.node_group
    image_node=group.nodes.new('CompositorNodeImage');image_node.image=image
    group.links.new(image_node.outputs['Image'],group.nodes['Effect Output'].inputs['Image'])
    source.show_expanded=False
    first=effects.create_effect(scene,levels.TYPE)
    second=effects.create_effect(scene,levels.TYPE)
    target,other=effects.effect_node(first),effects.effect_node(second)
    first.show_expanded=True
    second.show_expanded=False
    second.enable_for_preview=False;second.enable_for_render=False
    scene.compositor_effects.active=second
    area=next(a for a in window.screen.areas if a.type=='PROPERTIES')
    region=next(r for r in area.regions if r.type=='WINDOW')
    area.spaces.active.context='COMPOSITOR'
    for viewer in (a for a in window.screen.areas if a.type=='IMAGE_EDITOR'):
        viewer.spaces.active.show_region_ui=False
    for _ in range(4):yield
    helpers=runpy.run_path(str(Path(__file__).with_name('auto_level.py')),run_name='levels_helpers')
    yield from helpers['viewer_pixels_matches'](scene,helpers['reference'](pixels))
    yield from click(window,area.x+48,window.height-236)
    for _ in range(20):
        area.tag_redraw();yield
        if ui._cache.get(ui.key(target)) and ui._cache[ui.key(target)].sample:
            break
    shot('00-card-before-scope')
    assert ui._cache[ui.key(target)].sample.low==float(np.float32(.12))
    for _ in range(5):yield
    shot('01-card-auto')
    # An in-place image edit must invalidate a previously stable scope. Once
    # updated, an idle card must not keep capturing its whole input every tick.
    calls=[]
    capture=levels.capture
    def counted(*args):
        calls.append(time.monotonic())
        return capture(*args)
    levels.capture=counted
    try:
        changed=pixels.copy()
        changed[:,:,:3]=(.2+.5*pixels[:,:,:3])*pixels[:,:,3:4]
        image.pixels.foreach_set(changed.ravel());image.update();image.update_tag()
        yield from helpers['viewer_pixels_matches'](scene,helpers['reference'](changed))
        for _ in range(30):
            yield
            sample=ui._cache[ui.key(target)].sample
            if sample and abs(sample.low-.26)<1e-5:
                break
        assert abs(ui._cache[ui.key(target)].sample.low-.26)<1e-5
        for _ in range(10):yield
        stable=len(calls)
        for _ in range(20):yield
        assert len(calls)==stable,('Idle scope recaptured',stable,len(calls))
        image.pixels.foreach_set(pixels.ravel());image.update();image.update_tag()
        yield from helpers['viewer_pixels_matches'](scene,helpers['reference'](pixels))
        for _ in range(30):
            yield
            sample=ui._cache[ui.key(target)].sample
            if sample and abs(sample.low-.12)<1e-5:
                break
        assert abs(ui._cache[ui.key(target)].sample.low-.12)<1e-5
        print('PASS input histogram follows in-place pixels and stays idle without captures',flush=True)
    finally:
        levels.capture=capture
    with bpy.context.temp_override(window=window,area=area,region=region):
        bpy.ops.ed.undo_push(message='Levels UI fixture')
    # Enter from the actual card button while a different card is active.
    yield from click(window,area.x+125,window.height-550)
    for _ in range(4):yield
    assert ui._active and target.auto_level
    shot('02-histogram-auto')
    g=ui._active.geometry
    x,y,w,h=g['input']
    # Start precisely on the computed black marker, then drag along the input histogram.
    yield from event(window,'MOUSEMOVE','NOTHING',region.x+x+.12*w,region.y+y-5)
    yield from event(window,'LEFTMOUSE','PRESS',region.x+x+.12*w,region.y+y-5)
    yield from event(window,'MOUSEMOVE','NOTHING',region.x+x+.22*w,region.y+y-5)
    yield from event(window,'LEFTMOUSE','RELEASE',region.x+x+.22*w,region.y+y-5)
    assert not target.auto_level and abs(target.black_point-.22)<.01
    assert abs(target.white_point-.58)<1e-5
    assert other.auto_level and other.black_point==0 and other.white_point==1
    assert scene.compositor_effects.active==second
    shot('03-histogram-manual')
    # Auto ON immediately restores the dynamically measured markers.
    ax,ay,aw,ah=g['auto']
    yield from click(window,region.x+ax+aw/2,region.y+ay+ah/2)
    assert target.auto_level
    # Toggle back to manual retains the measured input endpoints.
    yield from click(window,region.x+ax+aw/2,region.y+ay+ah/2)
    assert not target.auto_level and abs(target.black_point-.12)<1e-5 and abs(target.white_point-.58)<1e-5
    # Esc rolls this whole interaction back, including mode.
    yield from event(window,'ESC','PRESS')
    yield from event(window,'ESC','RELEASE')
    assert ui._active is None and target.auto_level and target.black_point==0 and target.white_point==1
    with bpy.context.temp_override(window=window,area=area,region=region):
        bpy.ops.blender_raw.edit_levels('INVOKE_DEFAULT',tree_name=target.id_data.name,node_name=target.name)
    yield;yield
    g=ui._active.geometry;x,y,w,h=g['input']
    yield from event(window,'LEFTMOUSE','PRESS',region.x+x+.58*w,region.y+y-5)
    yield from event(window,'MOUSEMOVE','NOTHING',region.x+x+.72*w,region.y+y-5)
    yield from event(window,'LEFTMOUSE','RELEASE',region.x+x+.72*w,region.y+y-5)
    assert not target.auto_level and abs(target.white_point-.72)<.01
    dx,dy,dw,dh=g['done']
    yield from click(window,region.x+dx+dw/2,region.y+dy+dh/2)
    assert ui._active is None and not target.auto_level
    shot('04-card-manual')
    first_name=first.name
    with bpy.context.temp_override(window=window,area=area,region=region):
        assert bpy.ops.ed.undo()=={'FINISHED'}
    # Undo replaces RNA; locate the target again.
    target=effects.effect_node(window.scene.compositor_effects[first_name])
    assert target.auto_level
    print('PASS native Levels card, real black/white drags, Auto toggle, explicit target, cancel and Undo',flush=True)


steps=run()
deadline=time.monotonic()+90


def tick():
    try:
        assert time.monotonic()<deadline,'Levels UI test timed out'
        next(steps)
        return .12
    except StopIteration:
        bpy.ops.wm.quit_blender()
    except BaseException:
        traceback.print_exc();sys.stdout.flush();sys.stderr.flush();os._exit(1)
    return None


bpy.app.timers.register(tick,first_interval=.5)
