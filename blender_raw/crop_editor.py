# SPDX-License-Identifier: GPL-3.0-or-later
"""Modal crop and guide handles over the uncropped input in an Image Editor."""
import math
from contextlib import ExitStack

import bpy
from bpy.app.handlers import persistent
from bpy.props import StringProperty
import gpu
from gpu_extras.batch import batch_for_shader
import numpy as np

from . import crop_node, crop_geometry as geom, crop_detect

_active = None


def detection(image, direction='BOTH'):
    # Blender image copies need not preserve an unsaved buffer. Sample the actual input.
    w,h = image.size
    scale = min(1.,512/max(w,h))
    sw,sh = max(1,round(w*scale)),max(1,round(h*scale))
    if scale < 1 and image.source=='FILE' and not image.is_dirty:
        small = image.copy()
        try:
            small.scale(sw,sh)
            return detection(small, direction)
        finally:
            bpy.data.images.remove(small)
    data = np.empty(w*h*image.channels,dtype=np.float32)
    image.pixels.foreach_get(data)
    pixels = data.reshape(h,w,image.channels)
    ix = np.minimum(w-1, ((np.arange(sw)+.5)*w/sw).astype(int))
    iy = np.minimum(h-1, ((np.arange(sh)+.5)*h/sh).astype(int))
    rgb = pixels[iy[:,None],ix]
    gray = rgb[:,:,:3] @ np.array((.2126,.7152,.0722),dtype=np.float32) if image.channels>=3 else rgb[:,:,0]
    return crop_detect.detect(gray, direction)


def draw_overlay():
    op = _active
    ctx = bpy.context
    if not op or ctx.area!=op.area or ctx.window!=op.window or ctx.space_data.image!=op.image:
        return
    view = ctx.region.view2d
    shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    def line(points,color,width):
        vertices = [(*view.view_to_region(*pt,clip=False),0) for pt in points]
        batch = batch_for_shader(shader,'LINE_STRIP',{'pos':vertices})
        shader.bind()
        shader.uniform_float('viewportSize',gpu.state.viewport_get()[2:])
        for weight,tint in ((width+2,(.04,.04,.04,1)),(width,color)):
            shader.uniform_float('lineWidth',weight)
            shader.uniform_float('color',tint)
            batch.draw(shader)
    if op.action=='GUIDES':
        quad = np.reshape(op.p['guides'],(4,2))
        for i in range(4):
            direction = op.p.get('direction', 'BOTH')
            enabled = direction=='BOTH' or direction==('VERTICAL' if i%2 else 'HORIZONTAL')
            if enabled:
                line((quad[i],quad[(i+1)%4]),(.2,.85,1,1) if i%2 else (1,.68,.18,1),2)
    else:
        m = op.frame.inverse
        quad = geom.project(m,geom.UNIT)
        line([*quad,quad[0]],(1,1,1,1),2)
        for t in (1/3,2/3):
            for segment in (((0,t),(1,t)),((t,0),(t,1))):
                line(geom.project(m,segment),(.85,.85,.85,1),1)
    for pt in quad:
        x,y = view.view_to_region(*pt,clip=False)
        points = [view.region_to_view(x+dx,y+dy) for dx,dy in ((-5,-5),(5,-5),(5,5),(-5,5),(-5,-5))]
        line(points,(1,1,1,1),2)


@persistent
def stop(*_args):
    if _active:
        _active.cancel(None)


class BR_OT_edit_crop(bpy.types.Operator):
    bl_idname = 'blender_raw.edit_crop'
    bl_label = 'Edit Crop & Perspective'
    bl_options = {'UNDO','BLOCKING'}
    tree_name: StringProperty(options={'HIDDEN'})
    node_name: StringProperty(options={'HIDDEN'})
    action: StringProperty(options={'HIDDEN'})

    def invoke(self, context, event):
        global _active
        tree = bpy.data.node_groups.get(self.tree_name)
        self.node = tree.nodes.get(self.node_name) if tree else None
        if not self.node or self.node.bl_idname!=crop_node.TYPE or not tree.is_editable:
            return {'CANCELLED'}
        self.p = crop_node.parameters(self.node)
        if self.action in {'GUIDES','AUTO'}:
            # Direction selection prepares the next operation. Commit it only
            # together with accepted guides, so cancel/weak detection is inert.
            self.p['direction'] = self.node.edit_direction
            self.p['bounded_guides'] = True
        if self.action=='FIT':
            self.p.update(size=1.,position=(.5,.5))
            crop_node.apply(self.node,context,self.p,self.node.reference_aspect)
            return {'FINISHED'}
        if self.action=='OFF':
            self.node.mode = 'OFF'
            return {'FINISHED'}
        stop()
        self.resources = ExitStack()
        self.area = None
        self.original_type = None
        self.handle = None
        try:
            self.image = self.resources.enter_context(crop_node.reference(context.scene,self.node))
            self.aspect = self.image.size[0]/self.image.size[1]
            if self.action=='AUTO':
                self.p.update(guides=detection(self.image,self.p.get('direction','BOTH')),mode='AUTO')
                crop_node.apply(self.node,context,self.p,self.aspect)
                self.resources.close()
                self.report({'INFO'},'Guides applied. Use Edit Guides to refine them')
                return {'FINISHED'}
            areas = [a for a in context.screen.areas if a.type=='IMAGE_EDITOR']
            if areas:
                self.area = max(areas,key=lambda a:a.width*a.height)
            else:
                candidates = [a for a in context.screen.areas if a.type in {'NODE_EDITOR','VIEW_3D'}]
                self.area = max(candidates or [context.area],key=lambda a:a.width*a.height)
                self.original_type = self.area.ui_type
                self.area.type = 'IMAGE_EDITOR'
            self.window,self.screen,self.scene = context.window,context.screen,context.scene
            self.region = next(r for r in self.area.regions if r.type=='WINDOW')
            self.space = self.area.spaces.active
            self.previous = self.space.image
            self.space.image = self.image
            # A different reference can be much larger than the developed
            # preview. Fit it once; retain the user's view of the same image.
            if self.previous!=self.image:
                with context.temp_override(window=self.window,area=self.area,region=self.region):
                    bpy.ops.image.view_all(fit_view=True)
            if self.action=='GUIDES':
                self.p['mode'] = 'MANUAL'
                try:
                    self.frame = crop_node.solve(self.p,self.aspect)
                except ValueError:
                    self.p['guides'] = geom.GUIDES
                    self.frame = crop_node.solve(self.p,self.aspect)
            else:
                self.frame = crop_node.solve(self.p,self.aspect)
            self.drag = None
            self.handle = bpy.types.SpaceImageEditor.draw_handler_add(draw_overlay,(),'WINDOW','POST_PIXEL')
            _active = self
            guidance = {'BOTH':'Align the cyan verticals and amber horizontals',
                        'VERTICAL':'Align the two cyan vertical guides',
                        'HORIZONTAL':'Align the two amber horizontal guides'}
            self.area.header_text_set((guidance[self.p.get('direction','BOTH')] if self.action=='GUIDES'
                                       else 'Drag inside to move; drag a corner to resize; [ / ] to rotate')
                                      +'   |   Enter applies   Esc cancels')
            self.area.tag_redraw()
            context.window_manager.modal_handler_add(self)
            return {'RUNNING_MODAL'}
        except (RuntimeError, ValueError, OSError, ZeroDivisionError) as exc:
            self.cancel(context)
            self.report({'WARNING'},str(exc))
            return {'CANCELLED'}

    def cancel(self, context):
        global _active
        if _active is self:
            _active = None
        if self.handle is not None:
            bpy.types.SpaceImageEditor.draw_handler_remove(self.handle,'WINDOW')
            self.handle = None
        try:
            if self.area and self.area.type=='IMAGE_EDITOR':
                if self.space.image==self.image:
                    self.space.image = self.previous
                self.area.header_text_set(None)
                if self.original_type:
                    self.area.ui_type = self.original_type
                self.area.tag_redraw()
        except (ReferenceError,AttributeError):
            pass
        self.resources.close()

    def mouse(self,event):
        x,y = event.mouse_x-self.region.x,event.mouse_y-self.region.y
        return np.array(self.region.view2d.region_to_view(x,y)),np.array((x,y))

    def start_drag(self,uv,xy):
        f = self.frame
        quad = (np.reshape(self.p['guides'],(4,2)) if self.action=='GUIDES'
                else geom.project(f.inverse,geom.UNIT))
        pixels = [self.region.view2d.view_to_region(*pt,clip=False) for pt in quad]
        distances = np.linalg.norm(np.asarray(pixels)-xy,axis=1)
        index = int(np.argmin(distances))
        if distances[index] <= 16:
            self.drag = ('CORNER',index)
            self.drag_offset = quad[index]-uv
            self.drag_frame = f
        elif self.action=='CROP':
            work = geom.project(f.source_to_work,uv)[0]
            if np.all(work>=f.lower) and np.all(work<=f.lower+f.extent):
                self.drag = ('MOVE',work-f.center)

    def move(self,uv):
        f = self.frame
        p = self.p.copy()
        kind,arg = self.drag
        if kind=='CORNER':
            # Preserve where the handle was grabbed instead of snapping its
            # center to the cursor on the first movement.
            uv = uv+self.drag_offset
        if self.action=='GUIDES':
            p['guides'] = geom.drag_guides(p['guides'],arg,uv,self.aspect,
                                          axis=p.get('direction','BOTH'))
        else:
            work = geom.project(f.source_to_work,uv)[0]
            if kind=='MOVE':
                center = work-arg
            else:
                # Use the fixed opposite corner for the whole drag.
                start = self.drag_frame
                opposite = start.lower + geom.UNIT[(arg+2)%4]*start.extent
                signs = geom.UNIT[arg]*2-1
                distance = np.maximum(.002,(work-opposite)*signs)
                ratio = geom.ratio_for(p['preset'],self.aspect,p['free'],p['flip'])
                if p['preset']=='FREE':
                    ratio = float(np.clip(distance[0]/distance[1],.04,25))
                    p['free'] = 1/ratio if p['flip'] else ratio
                    height = distance[1]
                else:
                    height = (distance @ (ratio,1))/(ratio*ratio+1)
                fit = crop_node.solve(p,self.aspect)
                p['size'] = float(np.clip(height/fit.max_height,.01,1.))
                center = opposite + signs * (.5*fit.max_height*p['size'])*np.array((ratio,1))
            p['position'] = np.clip((center-f.lo)/(f.hi-f.lo),0,1).tolist()
        new_frame = crop_node.solve(p,self.aspect)
        self.p,self.frame = p,new_frame

    def modal(self,context,event):
        try:
            if (_active is not self or context.window.screen!=self.screen or context.window.scene!=self.scene
                    or self.area.type!='IMAGE_EDITOR' or self.space.image!=self.image
                    or event.type in {'ESC','RIGHTMOUSE','WINDOW_DEACTIVATE'}):
                self.cancel(context)
                return {'CANCELLED'}
            if event.type in {'RET','NUMPAD_ENTER'} and event.value=='PRESS':
                crop_node.apply(self.node,context,self.p,self.aspect)
                self.cancel(context)
                return {'FINISHED'}
            if self.action=='CROP' and event.type in {'LEFT_BRACKET','RIGHT_BRACKET'} and event.value=='PRESS':
                step = .1 if event.shift else 1.
                self.p['angle'] = float(np.clip(self.p['angle']+math.radians(step if event.type=='RIGHT_BRACKET' else -step),
                                               -math.pi, math.pi))
                self.frame = crop_node.solve(self.p,self.aspect)
            elif event.type=='LEFTMOUSE' and event.value=='RELEASE':
                try:
                    if self.drag is not None:
                        uv,_ = self.mouse(event)
                        self.move(uv)
                finally:
                    self.drag = None
            elif event.type in {'LEFTMOUSE','MOUSEMOVE'}:
                uv,xy = self.mouse(event)
                if event.type=='LEFTMOUSE' and event.value=='PRESS':
                    if np.all(xy>=0) and xy[0]<self.region.width and xy[1]<self.region.height:
                        self.start_drag(uv,xy)
                elif self.drag is not None:
                    self.move(uv)
            else:
                return {'PASS_THROUGH'}
            self.area.tag_redraw()
        except ValueError:
            # A crossing or singular guide drag keeps the last valid position.
            return {'RUNNING_MODAL'}
        except (RuntimeError,ReferenceError):
            self.cancel(context)
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}


def register():
    bpy.utils.register_class(crop_node.BR_NODE_crop)
    bpy.utils.register_class(BR_OT_edit_crop)
    for handlers in (bpy.app.handlers.load_pre,bpy.app.handlers.undo_pre,bpy.app.handlers.redo_pre):
        handlers.append(stop)


def unregister():
    stop()
    for handlers in (bpy.app.handlers.load_pre,bpy.app.handlers.undo_pre,bpy.app.handlers.redo_pre):
        handlers.remove(stop)
    bpy.utils.unregister_class(BR_OT_edit_crop)
    bpy.utils.unregister_class(crop_node.BR_NODE_crop)
