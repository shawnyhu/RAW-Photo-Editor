# SPDX-License-Identifier: GPL-3.0-or-later
"""Preview status anchored to Raw Developer and Spektrafilm compositor nodes."""

import blf
import bpy
import bpy.utils.previews
import gpu

from . import compositor, film_node, film_preview, gallery


_handle = None
_shader = None
_icons = None
_MAX_LABEL_CHARS = 200
_COLORS = {
    'IDLE': (.55, .58, .62, 1.0),
    'QUEUED': (.92, .67, .29, 1.0),
    'PROCESSING': (.37, .68, .98, 1.0),
    'READY': (.45, .76, .55, 1.0),
    'ERROR': (.98, .45, .42, 1.0),
}


def icon_id(phase):
    return _icons[phase].icon_id if _icons else 0


def _node_rect(node, view, scale):
    # location_absolute includes all parent frames. Blender's reported dimensions
    # are already in scaled canvas units, while the location is in node units.
    x, y = node.location_absolute
    x, y = x * scale, y * scale
    width, height = node.dimensions
    if node.hide:
        # Collapsing keeps the title centred on the original 20-unit header.
        y += height / 2 - 10 * scale
    left, top = view.view_to_region(x, y, clip=False)
    right, bottom = view.view_to_region(x + width, y - height, clip=False)
    return left, bottom, right, top


def _draw():
    context = bpy.context
    space, region = context.space_data, context.region
    if (space.tree_type != 'CompositorNodeTree' or not space.overlay.show_overlays
            or not space.edit_tree):
        return
    tree = space.edit_tree
    nodes = [node for node in tree.nodes if node.bl_idname in (compositor.NODE_TYPE, *film_node.FILM_TYPES)]
    if not nodes:
        return
    scale = context.preferences.system.ui_scale
    view = region.view2d
    rects = {node.name: _node_rect(node, view, scale) for node in tree.nodes
             if node.bl_idname != 'NodeFrame'}
    global _shader
    if _shader is None:
        _shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    previous = gpu.state.blend_get()
    try:
        gpu.state.blend_set('ALPHA')
        for node in nodes:
            x, _, right, top = rects[node.name]
            node_width = right - x
            if node_width < 36 * scale or right <= 0 or x >= region.width or top >= region.height:
                continue
            pixels_per_unit = node_width / node.width
            zoom = max(.65 * scale, min(pixels_per_unit, scale))
            height = round(19 * zoom)
            y = top + max(2, round(3 * zoom))
            if space.overlay.show_timing and not node.hide:
                y += round(20 * pixels_per_unit)
            if y + height < 0:
                continue
            status = (film_preview.preview_status(node) if node.bl_idname in film_node.FILM_TYPES
                      else compositor.developer_status(node))
            if not status:
                continue
            phase, text = status
            lines = text[:_MAX_LABEL_CHARS].splitlines()
            label = lines[0] if lines else ""
            if len(label) < len(text):
                label = label[:_MAX_LABEL_CHARS - 1].rstrip() + "…"
            size = max(8, round(12 * zoom))
            pad = max(3, round(5 * zoom))
            blf.size(0, size)
            width = min(node_width, blf.dimensions(0, label)[0] + 3 * pad)
            if any(name != node.name and bx < x + width and br > x and by < y + height and bt > y
                   for name, (bx, by, br, bt) in rects.items()):
                continue  # Never paint over another node's controls.
            gallery._rectangle(_shader, (x, y, width, height), (.08, .09, .10, .94))
            gallery._rectangle(_shader, (x, y, max(2, round(2 * zoom)), height), _COLORS[phase])
            gallery._label(label, x + 2 * pad, y + pad, size, width=width - 3 * pad)
    finally:
        gpu.state.blend_set(previous)


def register():
    global _handle, _icons
    if not bpy.app.background and _handle is None:
        _icons = bpy.utils.previews.new()
        transparent = (0., 0., 0., 0.)
        blank_row = transparent * 32
        for phase, color in _COLORS.items():
            preview = _icons.new(phase)
            preview.icon_size = (32, 32)
            bar_row = transparent * 14 + color * 4 + transparent * 14
            preview.icon_pixels_float = blank_row * 2 + bar_row * 28 + blank_row * 2
        _handle = bpy.types.SpaceNodeEditor.draw_handler_add(_draw, (), 'WINDOW', 'POST_PIXEL')


def unregister():
    global _handle, _shader, _icons
    if _handle is not None:
        bpy.types.SpaceNodeEditor.draw_handler_remove(_handle, 'WINDOW')
        _handle = None
    _shader = None
    if _icons is not None:
        bpy.utils.previews.remove(_icons)
        _icons = None
