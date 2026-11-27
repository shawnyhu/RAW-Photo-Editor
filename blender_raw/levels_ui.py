# SPDX-License-Identifier: GPL-3.0-or-later
"""Input/output scopes and draggable black/white points for native Auto Level."""
import time
from dataclasses import dataclass

import bpy
import bpy.utils.previews
from bpy.app.handlers import persistent
from bpy.props import StringProperty
import numpy as np

from . import levels_math, levels_node

_cache = {}
_interests = {}
_previews = None
_active = None
_registered = False
_capturing = False


@dataclass
class Scope:
    signature: object
    sample: object
    error: str = ''
    drawing: object = None
    dirty: bool = False
    scene_uid: int = 0
    retry_at: float = 0.


def key(node):
    return node.id_data.session_uid, node.as_pointer()


def signature(scene, node):
    from . import film_preview
    return film_preview._signature(node, scene, inputs_only=True)


def points(node, sample):
    if node.auto_level:
        return (levels_math.auto_points(sample.low, sample.high) if sample else None) or (0., 1.)
    return node.black_point, node.white_point


def domain(sample, black, white):
    # Auto uses the visible interval; manual endpoints can expand this axis.
    return min(0., black), max(1., white)


def histogram(values, limits, bins=256):
    counts = np.histogram(np.clip(values, *limits), bins=bins, range=limits)[0]
    # Square-root height makes the tails visible without changing their x positions.
    return np.sqrt(counts / max(1, counts.max()))


def distributions(node, sample, bins=256):
    black, white = points(node, sample)
    limits = domain(sample, black, white)
    output = (sample.values if node.auto_level and levels_math.auto_points(sample.low, sample.high) is None else
              np.clip((sample.values.astype(np.float64) - black) / (white - black), 0., 1.))
    return histogram(sample.values, limits, bins), histogram(output, (0., 1.), bins), limits


def _scene(context, node):
    from . import native_effects
    owner = native_effects.owner_effect(node)
    return owner[0] if owner else context.scene


def refresh(scene, node):
    """Refresh display data only. Automatic processing lives in the native graph."""
    global _capturing
    from . import film_node, film_preview, scene_pipeline
    sig, missing = signature(scene, node)
    if missing:
        raise ValueError('Waiting for the input preview')
    effect, parents = scene_pipeline.instance_for_node(scene, node, 'PREVIEW')
    if not effect:
        raise ValueError('Enable this card for preview to show its histogram')
    for _, upstream, _ in scene_pipeline.upstream(scene, (node.inputs['Image'],), effect=effect, parents=parents):
        if upstream.bl_idname in film_node.FILM_TYPES and upstream.live_preview:
            state, _ = film_preview.preview_status(upstream)
            if state in {'QUEUED', 'PROCESSING', 'ERROR'}:
                raise ValueError('Waiting for the upstream film preview')
    _capturing = True
    try:
        sample = levels_node.capture(scene, node)
    finally:
        _capturing = False
    _cache[key(node)] = Scope(sig, sample, scene_uid=scene.session_uid)
    # Bound memory when browsing a large photo library. These are disposable scopes.
    while len(_cache) > 8:
        stale = next(iter(_cache))
        del _cache[stale]
        if _previews is not None and str(stale) in _previews:
            del _previews[str(stale)]
    return sample


def current(scene, node):
    cached = _cache.get(key(node))
    sig, missing = signature(scene, node)
    return cached.sample if cached and cached.signature == sig and not cached.dirty and not missing and not cached.error else refresh(scene, node)


def _redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type in {'PROPERTIES', 'NODE_EDITOR', 'IMAGE_EDITOR'}:
                area.tag_redraw()


def _tick():
    if not _registered:
        return None
    if _active or bpy.app.is_job_running('RENDER') or bpy.app.is_job_running('COMPOSITE'):
        return .4
    now = time.monotonic()
    for ident, (scene, node, seen) in list(_interests.items()):
        sig = None
        try:
            if now - seen > 2. or not node.id_data or not scene.name:
                del _interests[ident]
                continue
            sig, missing = signature(scene, node)
            cached = _cache.get(ident)
            if cached and cached.signature == sig and not cached.dirty:
                if not cached.error or now < cached.retry_at:
                    continue
            if missing:
                continue
            refresh(scene, node)
        except ReferenceError:
            _interests.pop(ident, None)
            _cache.pop(ident, None)
            continue
        except (RuntimeError, ValueError, OSError) as exc:
            message = str(exc)
            _cache[ident] = Scope(sig, None, message,
                                  scene_uid=scene.session_uid,
                                  retry_at=now+1. if message.startswith('Waiting') else float('inf'))
        _redraw()
        break  # At most one whole-frame scope capture per timer tick.
    return .5


@persistent
def _composited(scene, *_args):
    # Generated/painted image buffers have no stable file identity. A completed
    # native recomposition is their invalidation signal; never poll pixels at idle.
    if _capturing:
        return
    changed = False
    for cached in _cache.values():
        if cached.scene_uid == scene.session_uid and cached.signature and not cached.signature[1]:
            cached.dirty = True
            changed = True
    if changed:
        _redraw()


@persistent
def _images_changed(_scene, graph):
    if not _capturing and any(isinstance(update.id, bpy.types.Image) for update in graph.updates):
        for cached in _cache.values():
            cached.dirty = True
        if _cache:
            _redraw()


def _preview(node, sample):
    """Raster scopes for the native card. The expanded editor handles dragging."""
    if _previews is None:
        return 0
    ident = str(key(node))
    entry = _cache[key(node)]
    drawing = (node.auto_level, *points(node, sample))
    preview = _previews.get(ident) or _previews.new(ident)
    if entry.drawing == drawing:
        return preview.icon_id
    size = 320
    pixels = np.full((size, size, 4), (.055, .055, .055, 1.), dtype=np.float32)
    before, after, limits = distributions(node, sample, 288)
    for values, base in ((before, 180), (after, 26)):
        for x in (16, 88, 160, 232, 303):
            pixels[base:base+116, x, :3] = .17
        for y in (base, base+58, base+115):
            pixels[y, 16:304, :3] = .17
        for x, value in enumerate(values, 16):
            pixels[base:base+max(1, int(value*110)), x, :3] = (.57, .64, .71)
    black, white = points(node, sample)
    for value, shade in ((black, .06), (white, .95)):
        x = int(16 + 287 * np.clip((value-limits[0])/(limits[1]-limits[0]), 0., 1.))
        pixels[180:296, max(0, x-1):x+2, :3] = (.95, .60, .20)
        for dy in range(9):
            pixels[171+dy, max(0,x-9+dy):min(size,x+10-dy), :3] = .7
        for dy in range(6):
            pixels[173+dy, max(0,x-6+dy):min(size,x+7-dy), :3] = shade
    # Distinguish the two graphs without embedding fonts in preview images.
    pixels[160:162, 16:304, :3] = .22
    preview.image_size = (size, size)
    preview.image_pixels_float = pixels.ravel()
    entry.drawing = drawing
    return preview.icon_id


def _button(layout, node, action, text, icon='NONE', **kwargs):
    op = layout.operator('blender_raw.edit_levels' if action == 'EDIT' else 'blender_raw.levels_action',
                         text=text, icon=icon, **kwargs)
    op.tree_name, op.node_name = node.id_data.name, node.name
    if action != 'EDIT':
        op.action = action


def draw(layout, node, context):
    scene = _scene(context, node)
    _interests[key(node)] = scene, node, time.monotonic()
    row = layout.row()
    row.use_property_split = False
    _button(row, node, 'TOGGLE', 'Auto Level', 'CHECKBOX_HLT' if node.auto_level else 'CHECKBOX_DEHLT',
            depress=node.auto_level)
    entry = _cache.get(key(node))
    sample = entry.sample if entry else None
    if sample:
        row = layout.row()
        row.alignment = 'CENTER'
        row.label(text='Input (top) / Output (bottom)')
        row = layout.row()
        row.alignment = 'CENTER'
        ui_scale = context.preferences.system.ui_scale
        width = min(280., max(150., context.region.width / ui_scale - 74.))
        row.template_icon(icon_value=_preview(node, sample), scale=width / 20.)
    else:
        layout.label(text=(entry.error if entry and entry.error else 'Reading input histogram…'), icon='INFO')
    row = layout.row(align=True)
    row.use_property_split = False
    _button(row, node, 'EDIT', 'Edit Histogram', 'FCURVE')
    _button(row, node, 'REFRESH', '', 'FILE_REFRESH')
    if node.auto_level:
        black, white = points(node, sample)
        for label, value in (('Black Point', black), ('White Point', white)):
            row = layout.split(factor=.4)
            left = row.row(); left.alignment = 'RIGHT'; left.label(text=label)
            right = row.row(); right.enabled = False
            right.label(text=f'{value:.5g}' if sample else 'Auto')
    else:
        layout.prop(node, 'black_point')
        layout.prop(node, 'white_point')
    if sample and node.auto_level and levels_math.auto_points(sample.low, sample.high) is None:
        layout.label(text='No visible range — kept unchanged', icon='INFO')
    row = layout.row(); row.enabled = False
    row.label(text='Linear RGB · Input preview scope')


def _target(op):
    tree = bpy.data.node_groups.get(op.tree_name)
    node = tree.nodes.get(op.node_name) if tree else None
    if not node or node.bl_idname != levels_node.TYPE or not tree.is_editable:
        raise ValueError('This Auto Level card is no longer available')
    return node


def _toggle(scene, node):
    if node.auto_level:
        sample = current(scene, node)
        levels_node.apply_points(node, *(levels_math.auto_points(sample.low, sample.high) or (0., 1.)))
    else:
        node.auto_level = True


class BR_OT_levels_action(bpy.types.Operator):
    bl_idname = 'blender_raw.levels_action'
    bl_label = 'Auto Level'
    bl_description = 'Toggle automatic levels, or drag black and white points over the input histogram'
    bl_options = {'REGISTER', 'UNDO'}
    tree_name: StringProperty(options={'HIDDEN'})
    node_name: StringProperty(options={'HIDDEN'})
    action: StringProperty(options={'HIDDEN'})

    def execute(self, context):
        try:
            node = _target(self)
            scene = _scene(context, node)
            if self.action == 'TOGGLE':
                _toggle(scene, node)
            elif self.action == 'REFRESH':
                refresh(scene, node)
            _redraw()
            return {'FINISHED'}
        except (ValueError, RuntimeError, OSError) as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}


def editor_geometry(width, height):
    """Local region pixels, also used by the real-window interaction regression."""
    w = min(540., width-24.)
    h = min(440., height-32.)
    x, y = (width-w)/2., (height-h)/2.
    return dict(panel=(x,y,w,h), input=(x+22,y+h*.53,w-44,h*.28),
                output=(x+22,y+h*.16,w-44,h*.23),
                auto=(x+16,y+12,w*.48-16,27), done=(x+w*.54,y+12,w*.46-16,27))


def _inside(rect, x, y):
    rx, ry, w, h = rect
    return rx <= x <= rx+w and ry <= y <= ry+h


def _draw_editor():
    import blf
    import gpu
    from gpu_extras.batch import batch_for_shader
    op = _active
    if not op or bpy.context.area != op.area or bpy.context.region != op.region:
        return
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')

    def shape(vertices, color, kind='TRIS'):
        shader.bind(); shader.uniform_float('color', color)
        batch_for_shader(shader, kind, {'pos': vertices}).draw(shader)

    def rect(box, color):
        x,y,w,h = box
        shape(((x,y),(x+w,y),(x+w,y+h),(x,y),(x+w,y+h),(x,y+h)), color)

    def text(x,y,label,size=12,color=(.82,.82,.82,1)):
        blf.size(0,size); blf.position(0,x,y,0); blf.color(0,*color); blf.draw(0,label)

    g = editor_geometry(op.region.width, op.region.height)
    op.geometry = g
    x,y,w,h = g['panel']
    gpu.state.blend_set('ALPHA')
    rect((x-2,y-2,w+4,h+4),(.05,.05,.05,1))
    rect(g['panel'],(.13,.13,.13,1))
    text(x+18,y+h-26,'Auto Level' if op.node.auto_level else 'Manual Levels',16,(.96,.96,.96,1))
    text(x+18,y+h-45,'Drag a point to switch to Manual',11)
    before, after, limits = distributions(op.node,op.sample)
    for name, values, axis, title in (('input',before,limits,'Input · Linear RGB'),
                                       ('output',after,(0.,1.),'Output')):
        rx,ry,rw,rh = g[name]
        rect(g[name],(.045,.045,.045,1))
        for fraction in (0.,.25,.5,.75,1.):
            rect((rx+fraction*rw,ry,1,rh),(.2,.2,.2,1))
        vertices=[]
        for i,value in enumerate(values):
            a,b=rx+i*rw/len(values),rx+(i+1)*rw/len(values)
            top=ry+float(value)*(rh-3)
            vertices.extend(((a,ry),(b,ry),(b,top),(a,ry),(b,top),(a,top)))
        shape(vertices,(.58,.67,.75,1))
        text(rx,ry+rh+7,title)
        text(rx,ry-17,f'{axis[0]:.4g}',10)
        label=f'{axis[1]:.4g}'
        text(rx+rw-7*len(label),ry-17,label,10)
    black,white=points(op.node,op.sample)
    rx,ry,rw,rh=g['input']
    for value,label,shade in ((black,'B',.05),(white,'W',.95)):
        px=rx+rw*float(np.clip((value-limits[0])/(limits[1]-limits[0]),0.,1.))
        rect((px-.7,ry,1.4,rh),(1,.62,.22,1))
        shape(((px,ry+1),(px-8,ry-11),(px+8,ry-11)),(.75,.75,.75,1))
        shape(((px,ry-2),(px-5,ry-9),(px+5,ry-9)),(shade,shade,shade,1))
    text(rx,ry-35,f'Black {black:.5g}    White {white:.5g}',11)
    for name,label in (('auto','Auto Level: ON' if op.node.auto_level else 'Auto Level: OFF'),('done','Done')):
        rect(g[name],(.21,.38,.53,1) if name=='auto' and op.node.auto_level else (.23,.23,.23,1))
        bx,by,bw,bh=g[name]; text(bx+9,by+8,label,11)
    gpu.state.blend_set('NONE')


class BR_OT_edit_levels(bpy.types.Operator):
    bl_idname = 'blender_raw.edit_levels'
    bl_label = 'Edit Levels Histogram'
    bl_description = 'Drag the black or white point; Enter applies and Esc restores the previous settings'
    bl_options = {'UNDO', 'BLOCKING'}
    tree_name: StringProperty(options={'HIDDEN'})
    node_name: StringProperty(options={'HIDDEN'})

    def invoke(self, context, event):
        global _active
        self.handle = None
        try:
            stop()
            self.node = _target(self)
            self.scene = _scene(context,self.node)
            self.sample = current(self.scene,self.node)
            self.original = self.node.auto_level,self.node.black_point,self.node.white_point
            self.area,self.region = context.area,context.region
            if self.region.type != 'WINDOW':
                self.region = next(r for r in self.area.regions if r.type=='WINDOW')
            self.space_type = getattr(bpy.types, {'PROPERTIES':'SpaceProperties','NODE_EDITOR':'SpaceNodeEditor',
                                                'IMAGE_EDITOR':'SpaceImageEditor'}.get(self.area.type,''),None)
            if self.space_type is None:
                raise ValueError('Open Levels from its effect card or compositor node')
            self.drag = None
            self.geometry = editor_geometry(self.region.width,self.region.height)
            self.handle = self.space_type.draw_handler_add(_draw_editor,(),'WINDOW','POST_PIXEL')
            _active = self
            context.window_manager.modal_handler_add(self)
            self.area.tag_redraw()
            return {'RUNNING_MODAL'}
        except (ValueError,RuntimeError,OSError) as exc:
            self.report({'WARNING'},str(exc))
            return {'CANCELLED'}

    def finish(self, restore=False):
        global _active
        if restore:
            try:
                auto,black,white=self.original
                levels_node.apply_points(self.node,black,white)
                self.node.auto_level=auto
            except (ReferenceError, RuntimeError):
                pass
        if self.handle is not None:
            self.space_type.draw_handler_remove(self.handle,'WINDOW')
            self.handle=None
        if _active is self:
            _active=None
        _redraw()

    def cancel(self, context):
        self.finish(restore=True)

    def modal(self, context, event):
        try:
            if context.scene != self.scene or _target(self) != self.node:
                self.finish(restore=True)
                return {'CANCELLED'}
            if event.type in {'ESC','RIGHTMOUSE'} and event.value=='PRESS':
                self.finish(restore=True)
                return {'CANCELLED'}
            if event.type in {'RET','NUMPAD_ENTER'} and event.value=='PRESS':
                self.finish()
                return {'FINISHED'}
            x,y=event.mouse_x-self.region.x,event.mouse_y-self.region.y
            if event.type=='LEFTMOUSE' and event.value=='PRESS':
                if _inside(self.geometry['done'],x,y):
                    self.finish()
                    return {'FINISHED'}
                if _inside(self.geometry['auto'],x,y):
                    _toggle(self.scene,self.node)
                else:
                    rx,ry,rw,rh=self.geometry['input']
                    if rx-10<=x<=rx+rw+10 and ry-18<=y<=ry+rh:
                        black,white=points(self.node,self.sample)
                        limits=domain(self.sample,black,white)
                        candidates=[rx+rw*(v-limits[0])/(limits[1]-limits[0]) for v in (black,white)]
                        self.drag=int(np.argmin(np.abs(np.asarray(candidates)-x)))
                        self.drag_domain=limits
                        levels_node.apply_points(self.node,black,white)
                        self.move(x)
            elif event.type=='MOUSEMOVE' and self.drag is not None:
                self.move(x)
            elif event.type=='LEFTMOUSE' and event.value=='RELEASE':
                self.drag=None
            self.area.tag_redraw()
            return {'RUNNING_MODAL'}
        except (ReferenceError,ValueError,RuntimeError):
            self.finish()
            return {'CANCELLED'}

    def move(self,x):
        rx,ry,rw,rh=self.geometry['input']
        low,high=self.drag_domain
        value=low+float(np.clip((x-rx)/rw,0.,1.))*(high-low)
        if self.drag==0:
            self.node.black_point=value
        else:
            self.node.white_point=value


@persistent
def stop(*_args):
    if _active:
        _active.finish(restore=True)


@persistent
def clear(*_args):
    stop()
    _cache.clear()
    _interests.clear()
    if _previews is not None:
        _previews.clear()


def register():
    global _previews,_registered
    for cls in (BR_OT_levels_action,BR_OT_edit_levels):
        bpy.utils.register_class(cls)
    if not bpy.app.background:
        _previews=bpy.utils.previews.new()
        _registered=True
        bpy.app.timers.register(_tick,first_interval=.5)
    bpy.app.handlers.load_pre.append(clear)
    bpy.app.handlers.undo_pre.append(stop)
    bpy.app.handlers.redo_pre.append(stop)
    bpy.app.handlers.undo_post.append(clear)
    bpy.app.handlers.redo_post.append(clear)
    bpy.app.handlers.composite_post.append(_composited)
    bpy.app.handlers.depsgraph_update_post.append(_images_changed)


def unregister():
    global _previews,_registered
    _registered=False
    clear()
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    if _previews is not None:
        bpy.utils.previews.remove(_previews)
        _previews=None
    bpy.app.handlers.redo_pre.remove(stop)
    bpy.app.handlers.undo_post.remove(clear)
    bpy.app.handlers.redo_post.remove(clear)
    bpy.app.handlers.composite_post.remove(_composited)
    bpy.app.handlers.depsgraph_update_post.remove(_images_changed)
    bpy.app.handlers.undo_pre.remove(stop)
    bpy.app.handlers.load_pre.remove(clear)
    for cls in (BR_OT_edit_levels,BR_OT_levels_action):
        bpy.utils.unregister_class(cls)
