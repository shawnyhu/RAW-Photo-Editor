# SPDX-License-Identifier: GPL-3.0-or-later
"""Place the Raw Developer's circular meter in the current Image Editor view."""
import math

import bpy
from bpy.app.handlers import persistent
from bpy.props import StringProperty
import gpu
from gpu_extras.batch import batch_for_shader


_handle = None
_picker = None
_circle = tuple((math.cos(math.tau*i/80), math.sin(math.tau*i/80)) for i in range(81))


def draw_controls(layout, node, photo, *, effect=None):
    from . import native_effects
    layout.label(text="Auto Exposure")
    socket = node.inputs.get('Exposure')
    if not effect and node.id_data.get(native_effects.KIND_TAG):
        owner = native_effects.owner_effect(node)
        if owner and native_effects.effect_node(owner[1]) == node:
            effect = owner[1]
    prop = native_effects.input_property(effect, 'Exposure') if effect else None
    row = layout.row()
    row.use_property_decorate = True
    if prop:
        # Keep the exact native animation/driver path used by the original card.
        row.prop(prop, 'value', text='Exposure')
    elif socket and not socket.is_linked:
        row.prop(socket, 'default_value', text='Exposure')
    elif socket:
        row.label(text='Exposure (linked)', icon='LINKED')
    else:
        row.prop(node, 'exposure')

    column = layout.column(align=True)
    column.enabled = bool(photo)
    column.prop(node, 'as_shot')
    if node.as_shot:
        return
    column.prop(node, 'meter_mode')
    column.label(text=_exposure_readout(node, photo))
    if node.auto_exposure == 'SPOT':
        row = column.row(align=True)
        op = row.operator(BR_OT_pick_meter.bl_idname, text='Pick Area', icon='EYEDROPPER')
        op.tree_name, op.node_name = node.id_data.name, node.name
        row.prop(node, 'spot_radius', text='Radius')


def _exposure_readout(node, photo):
    from . import compositor, jobs
    if node.auto_exposure == 'AS_SHOT':
        return 'auto exposure off'
    meter = node.node_tree.nodes.get('Meter Offset') if node.node_tree else None
    if (not meter or not photo or jobs.preview_phase(photo) or jobs.preview_phase(photo, node)
            or (compositor.uses_camera_adjustments(node) and node.get('recipe_dirty'))
            or not compositor._meter_image(node)):
        return 'Auto exposure: awaiting image'
    # Read the correction already applied to the preview. Drawing must not re-meter the RAW.
    ev = round(meter.inputs[1].default_value, 2)
    return f'Auto exposure: {ev or 0.0:+.2f} EV'


def _matches(area, scene, node):
    from . import compositor, scene_pipeline
    image = area.spaces.active.image
    target = compositor._meter_image(node)
    photo = compositor.developer_source(node)
    if not target or not photo or not image or not all(target.size) or tuple(target.size) != tuple(image.size):
        return False
    if image == target or image == photo.image:
        return True
    if image.type != 'COMPOSITING' or (scene.get(compositor.STUDIO_TAG) and scene.get(compositor.PHOTO_TAG) != photo.uid):
        return False
    try:
        effect, _parents = scene_pipeline.instance_for_node(scene, node, mode='PREVIEW')
    except ValueError:
        return False
    return bool(effect and not compositor._has_spatial_edit(effect.node_group))


def _draw_circle():
    context = bpy.context
    picker = _picker
    if (picker is None or not picker.valid() or context.window != picker.window
            or context.scene != picker.scene or context.area != picker.area):
        return
    node, position = picker.node, picker.position
    if position is None:
        return
    width, height = context.space_data.image.size
    if min(width, height) < 1:
        return
    radius = min(width, height) * node.spot_radius / 100
    u, v = position
    coords = [context.region.view2d.view_to_region(u+x*radius/width, 1-v+y*radius/height, clip=False) for x,y in _circle]
    shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    batch = batch_for_shader(shader, 'LINE_STRIP', {'pos': [(x,y,0) for x,y in coords]})
    shader.bind()
    shader.uniform_float('viewportSize', gpu.state.viewport_get()[2:])
    for line_width, color in ((3.0, (0.03, 0.03, 0.03, 1.0)), (1.0, (1.0, 1.0, 1.0, 1.0))):
        shader.uniform_float('lineWidth', line_width)
        shader.uniform_float('color', color)
        batch.draw(shader)


class BR_OT_pick_meter(bpy.types.Operator):
    bl_idname = 'blender_raw.pick_meter'
    bl_label = 'Pick Metering Area'
    bl_description = 'Click the spot to meter in the Image Editor'
    bl_options = {'UNDO', 'BLOCKING'}
    tree_name: StringProperty(options={'HIDDEN'})
    node_name: StringProperty(options={'HIDDEN'})

    def invoke(self, context, event):
        from . import compositor
        global _picker, _handle
        tree = bpy.data.node_groups.get(self.tree_name)
        node = tree.nodes.get(self.node_name) if tree else None
        if (not context.window or not node or node.bl_idname != compositor.NODE_TYPE
                or node.auto_exposure != 'SPOT' or not node.id_data.is_editable):
            return {'CANCELLED'}
        areas = [a for a in context.window.screen.areas if a.type == 'IMAGE_EDITOR' and _matches(a, context.scene, node)]
        if not areas:
            self.report({'WARNING'}, 'Show the developed photo or connect its Viewer before cropping or warping')
            return {'CANCELLED'}
        if _picker is not None:
            _picker.cancel(None)
        self.area = max(areas, key=lambda a: a.width*a.height)
        self.node, self.image, self.position = node, self.area.spaces.active.image, tuple(node.spot_center)
        self.window, self.screen, self.scene = context.window, context.window.screen, context.scene
        self.window_manager = context.window_manager
        self.photo_uid = compositor.developer_source(node).uid
        self._ended, self._cursor_set, self._timer = False, False, None
        _picker = self
        try:
            _handle = bpy.types.SpaceImageEditor.draw_handler_add(_draw_circle, (), 'WINDOW', 'POST_PIXEL')
            self.area.header_text_set('Click to place the circular meter. Esc cancels.')
            self.area.tag_redraw()
            self.window.cursor_modal_set('CROSSHAIR')
            self._cursor_set = True
            self._timer = self.window_manager.event_timer_add(.1, window=self.window)
            self.window_manager.modal_handler_add(self)
        except Exception:
            self.cancel(None)
            raise
        return {'RUNNING_MODAL'}

    def valid(self):
        from . import compositor
        try:
            if (self._ended or self.window.screen != self.screen or self.window.scene != self.scene
                    or not any(area == self.area for area in self.screen.areas) or self.area.type != 'IMAGE_EDITOR'
                    or self.area.spaces.active.image != self.image or self.node.auto_exposure != 'SPOT'
                    or not self.node.id_data.is_editable):
                return False
            photo = compositor.developer_source(self.node)
            return bool(photo and photo.uid == self.photo_uid and _matches(self.area, self.scene, self.node))
        except (ReferenceError, RuntimeError):
            return False

    def cancel(self, context):
        global _picker, _handle
        if getattr(self, '_ended', False):
            return
        self._ended = True
        if _picker is self:
            _picker = None
            if _handle is not None:
                bpy.types.SpaceImageEditor.draw_handler_remove(_handle, 'WINDOW')
                _handle = None
        if self._timer is not None:
            try:
                self.window_manager.event_timer_remove(self._timer)
            except (ReferenceError, RuntimeError):
                pass
            self._timer = None
        if self._cursor_set:
            try:
                self.window.cursor_modal_restore()
            except (ReferenceError, RuntimeError):
                pass
            self._cursor_set = False
        try:
            self.area.header_text_set(None)
            self.area.tag_redraw()
        except (ReferenceError, RuntimeError):
            pass

    def modal(self, context, event):
        from . import compositor
        if _picker is not self or not self.valid():
            self.cancel(context)
            return {'CANCELLED'}
        if event.type in {'ESC', 'RIGHTMOUSE', 'WINDOW_DEACTIVATE'}:
            self.cancel(context)
            return {'CANCELLED'}
        if event.type not in {'MOUSEMOVE', 'LEFTMOUSE'}:
            return {'PASS_THROUGH'}
        try:
            self.position = compositor._sample_position(self.area, event.mouse_x, event.mouse_y)
            self.area.tag_redraw()
            if self.position is not None and event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                self.node.spot_center = self.position
                self.cancel(context)
                return {'FINISHED'}
        except ReferenceError:
            self.cancel(context)
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}


@persistent
def _cancel_picker(*_args):
    if _picker is not None:
        _picker.cancel(None)


_handlers = ('load_pre', 'undo_pre', 'redo_pre')


def register():
    bpy.utils.register_class(BR_OT_pick_meter)
    for name in _handlers:
        getattr(bpy.app.handlers, name).append(_cancel_picker)


def unregister():
    global _handle, _picker
    _cancel_picker()
    if _handle is not None:
        bpy.types.SpaceImageEditor.draw_handler_remove(_handle, 'WINDOW')
        _handle = None
    _picker = None
    for name in _handlers:
        handlers = getattr(bpy.app.handlers, name)
        if _cancel_picker in handlers:
            handlers.remove(_cancel_picker)
    bpy.utils.unregister_class(BR_OT_pick_meter)
    from . import metering
    metering._packed_meter_file.cache_clear()
