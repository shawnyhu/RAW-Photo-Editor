# SPDX-License-Identifier: GPL-3.0-or-later
"""Workspace-local canvas color outside the Image Editor's displayed image."""

import bpy
from bpy.props import EnumProperty
import gpu
from gpu_extras.batch import batch_for_shader


COLORS = {
    'BLACK': (0., 0., 0., 1.),
    'DARK': (.12, .12, .12, 1.),
    'LIGHT': (.75, .75, .75, 1.),
}
_handle = None
_shader = None


def is_viewer(context):
    space = context.space_data
    return bool(context.workspace and context.workspace.get('blender_raw_workspace')
                and space and space.type == 'IMAGE_EDITOR' and space.mode == 'VIEW')


def _redraw(workspace, context):
    manager = getattr(context, 'window_manager', None)
    if manager:
        for window in manager.windows:
            if window.workspace == workspace:
                for area in window.screen.areas:
                    if area.type == 'IMAGE_EDITOR':
                        area.tag_redraw()


def image_bounds(region):
    """Return subpixel image bounds using the native pan/zoom/aspect transform."""
    width, height = region.width, region.height
    u0, v0 = region.view2d.region_to_view(0, 0)
    u1, v1 = region.view2d.region_to_view(width, height)
    if abs(u1-u0) < 1e-12 or abs(v1-v0) < 1e-12:
        return None
    sx, sy = width/(u1-u0), height/(v1-v0)
    x0, x1 = sorted((-u0*sx, (1-u0)*sx))
    y0, y1 = sorted((-v0*sy, (1-v0)*sy))
    return x0, y0, x1, y1


def outside_rectangles(width, height, bounds):
    """Fill only canvas pixels; leave the image and its alpha display untouched."""
    if bounds is None:
        return [(0., 0., width, height)]
    x0, y0, x1, y1 = bounds
    x0, x1 = (max(0., min(width, x)) for x in (x0, x1))
    y0, y1 = (max(0., min(height, y)) for y in (y0, y1))
    return [(0., 0., width, y0), (0., y1, width, height),
            (0., y0, x0, y1), (x1, y0, width, y1)]


def draw_background():
    global _shader
    context = bpy.context
    if not is_viewer(context) or context.region.type != 'WINDOW':
        return
    space, region = context.space_data, context.region
    image = space.image
    # Repeated/UDIM textures can occupy pixels beyond the primary image tile.
    if space.show_repeat or (image and image.source == 'TILED'):
        return
    bounds = image_bounds(region) if image and min(image.size) > 0 else None
    vertices = []
    for x0, y0, x1, y1 in outside_rectangles(region.width, region.height, bounds):
        if x1 > x0 and y1 > y0:
            vertices.extend(((x0,y0), (x1,y0), (x1,y1),
                             (x0,y0), (x1,y1), (x0,y1)))
    if not vertices:
        return
    if _shader is None:
        _shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    batch = batch_for_shader(_shader, 'TRIS', {'pos': vertices})
    previous_blend = gpu.state.blend_get()
    previous_depth = gpu.state.depth_test_get()
    try:
        gpu.state.blend_set('NONE')
        gpu.state.depth_test_set('NONE')
        _shader.bind()
        _shader.uniform_float('color', COLORS[context.workspace.photo_editor_canvas_background])
        batch.draw(_shader)
    finally:
        gpu.state.depth_test_set(previous_depth)
        gpu.state.blend_set(previous_blend)


class BR_PT_viewer_background(bpy.types.Panel):
    bl_label = 'Canvas Background'
    bl_idname = 'BR_PT_viewer_background'
    bl_space_type = 'IMAGE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'Editor'

    @classmethod
    def poll(cls, context):
        return is_viewer(context)

    def draw(self, context):
        self.layout.prop(context.workspace, 'photo_editor_canvas_background', expand=True)


def register():
    global _handle
    bpy.types.WorkSpace.photo_editor_canvas_background = EnumProperty(
        name='Canvas Background',
        description='Background outside the photo in this workspace',
        items=(('BLACK', 'Black', 'Pure black canvas'),
               ('DARK', 'Dark Gray', 'Dark gray canvas'),
               ('LIGHT', 'Light Gray', 'Light gray canvas')),
        default='BLACK', update=_redraw)
    bpy.utils.register_class(BR_PT_viewer_background)
    if not bpy.app.background:
        _handle = bpy.types.SpaceImageEditor.draw_handler_add(draw_background, (), 'WINDOW', 'POST_PIXEL')


def unregister():
    global _handle, _shader
    if _handle is not None:
        bpy.types.SpaceImageEditor.draw_handler_remove(_handle, 'WINDOW')
        _handle = None
    _shader = None
    bpy.utils.unregister_class(BR_PT_viewer_background)
    del bpy.types.WorkSpace.photo_editor_canvas_background
