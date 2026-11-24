# SPDX-License-Identifier: GPL-3.0-or-later
"""Photo Gallery editor subtype, canvas, input handling and studio workspace."""

from array import array
from collections import OrderedDict
from functools import wraps
from math import cos, pi, sin
from pathlib import Path
from typing import NamedTuple

import blf
import bpy
import gpu
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty
from gpu_extras.batch import batch_for_shader

from . import adjustments, catalog, compositor, edited_preview, filters


GALLERY_TYPE = "BlenderRawGalleryTree"
GALLERY_NAME = "Photo Gallery"
WORKSPACE_NAME = "RAW Photo Editor"
_offsets = {}
_scroll_grids = {}
_scroll_proximity = {}
_hover_window = None
_pressed_bars = set()
_view_geometry = {}
_anchors = {}
_context_photos = {}
_textures = OrderedDict()
_keymaps = []
_timers = set()
_handle = None
_solid = None
_image_shader = None
_boxes = {}
_header_override = None
_ordinary_regions = {}
_region_modes = {}
_gallery_regions = {}
_new_sidebars = set()
_status_regions = {}
_panel_timers = {}
_compositor_layout_timers = {}
_navigation_timers = {}
_workspace_subscription = object()


def _is_gallery(context):
    space = context.space_data
    return space and space.type == 'NODE_EDITOR' and space.tree_type == GALLERY_TYPE


class BlenderRawGalleryTree(bpy.types.NodeTree):
    """Use Blender's supported custom editor subtype registration."""

    bl_idname = GALLERY_TYPE
    bl_label = GALLERY_NAME
    bl_icon = 'IMAGE_DATA'
    bl_description = "Import, browse, rate and export the photographs in this project"


def _filtered(library):
    return filters.filtered(library)


def _key(context):
    return context.area.as_pointer()


def _thumbnail_limit(context, size):
    addon = context.preferences.addons.get(__package__)
    if addon and addon.preferences:
        return getattr(addon.preferences, "thumbnail_" + size.lower())
    return catalog.THUMBNAIL_DEFAULTS[size]


class _Grid(NamedTuple):
    width: int
    height: int
    scale: float
    pad: int
    card_w: int
    card_h: int
    caption: int
    columns: int
    row_step: int
    offset: int
    maximum: int
    left: int
    scroll_width: int
    scroll_inset: int

    @property
    def scroll_left(self):
        return self.left + self.width - self.scroll_inset - self.scroll_width

    @property
    def idle_width(self):
        return max(1, self.scroll_width - round(4 * self.scale))

    @property
    def track_inset(self):
        return min(int(2 * self.scale), max(0, (self.height - 1) // 2))

    def proximity(self, x, y):
        if not self.scroll_width or not (self.left <= x < self.left + self.width and 0 <= y < self.height):
            return 0.0
        # View2D expands over a header-height approach zone. Measure against
        # the idle and expanded edges so the result is independent of event rate.
        reach = 26 * self.scale - (self.scroll_width - self.idle_width)
        return max(0.0, min(1.0, 1.0 - (self.scroll_left - x) / reach))

    @property
    def thumb(self):
        """Scrollbar grip bottom and height in WINDOW pixels."""
        track = self.height - 2 * self.track_inset
        size = round(track * self.height / max(1, self.height + self.maximum))
        size = min(track, max(round(30 * self.scale), size))
        travel = track - size
        return self.track_inset + travel - round(travel * self.offset / max(1, self.maximum)), size


def _reveal_row(grid, position, *, center=False):
    top = grid.pad + position // grid.columns * grid.row_step
    bottom = top + grid.card_h + grid.caption
    margin = min(grid.pad, max(0, (grid.height - (bottom - top)) // 2))
    if grid.offset <= top and bottom <= grid.offset + grid.height:
        return grid.offset
    if center or bottom - top > grid.height:
        # When only part of a thumbnail fits, center the photograph itself.
        span = grid.card_h if bottom - top > grid.height else bottom - top
        offset = top + (span - grid.height) // 2
    elif top < grid.offset:
        offset = top - margin
    else:
        offset = bottom + margin - grid.height
    return max(0, min(offset, grid.maximum))


def _layout(context, shown):
    # In overlap mode the two open side regions occupy part of the WINDOW.
    region = context.region
    left, right = 0, region.width
    for side in context.area.regions:
        if side.width <= 2 or side.height <= 2:
            continue
        if side.type == 'TOOLS' and context.space_data.show_region_toolbar and side.x <= region.x < side.x + side.width:
            left = min(right, side.x + side.width - region.x)
        elif side.type == 'UI' and context.space_data.show_region_ui and region.x <= side.x < region.x + region.width:
            right = side.x - region.x
    width = max(1, right - left)
    scale = max(0.75, context.preferences.system.ui_scale or 1.0)
    pad = round(8 * scale)
    lib = catalog.library(context)
    min_side = round(50 * scale)
    max_side = round(_thumbnail_limit(context, lib.thumbnail_scale) * scale)
    # Allow one gap of spare width before wrapping a full-sized row.
    span = max_side + pad
    columns = max(1, min((width - 2 * pad + span - 1) // span,
                         (width - pad) // (min_side + pad)))
    side = max(min_side, min(max_side, (width - (columns + 1) * pad) // columns))
    caption = round(20 * scale)
    row_step = side + caption + pad
    height = region.height
    rows = (len(shown) + columns - 1) // columns
    max_offset = max(0, rows * row_step + pad - height) if rows else 0
    pixel = max(1, round(context.preferences.system.pixel_size))
    scroll_inset = min(width, int(2 * scale) + pixel)
    # View2D's full scroller includes the outer padding and theme outline.
    scroll_width = min(width - scroll_inset, int(9 * scale) + 2 * pixel - scroll_inset) if max_offset else 0
    key = _key(context)
    offset = max(0, min(_offsets.get(key, 0), max_offset))
    grid = _Grid(width, height, scale, pad, side, side, caption, columns, row_step, offset,
                 max_offset, left, scroll_width, scroll_inset)
    filter_settings = (lib.query, lib.min_rating, lib.flag_filter, lib.format_filter,
                       lib.date_filter, lib.date_start, lib.date_end)
    geometry = (width, height, columns, side, lib.active_index, len(shown), filter_settings)
    previous = _view_geometry.get(key)
    if previous != geometry:
        _view_geometry[key] = geometry
        position = next((i for i, (index, _) in enumerate(shown) if index == lib.active_index), None)
        if position is not None:
            offset = _reveal_row(grid, position, center=True)
        elif previous and previous[-1] != filter_settings:
            offset = 0
        grid = grid._replace(offset=offset)
    _offsets[key] = offset
    return grid


def _visible_cards(shown, layout):
    first = layout.offset // layout.row_step * layout.columns
    grid_width = layout.columns * layout.card_w + (layout.columns - 1) * layout.pad
    left = layout.left + (layout.width - grid_width) // 2
    for position in range(first, len(shown)):
        row, col = divmod(position, layout.columns)
        top = layout.height + layout.offset - layout.pad - row * layout.row_step
        if top <= 0:
            break
        x = left + col * (layout.card_w + layout.pad)
        index, photo = shown[position]
        yield index, photo, x, top - layout.card_h


def _hit_cards(context, bounds):
    shown = _filtered(catalog.library(context))
    layout = _layout(context, shown)
    x0, y0, x1, y1 = bounds
    if layout.scroll_left <= x0 <= x1 < layout.scroll_left + layout.scroll_width:
        return []
    x0, x1 = max(layout.left, x0), min(layout.left + layout.width - 1, x1)
    y0, y1 = max(0, y0), min(layout.height - 1, y1)
    if x0 > x1 or y0 > y1:
        return []
    return [(i, photo) for i, photo, x, y in _visible_cards(shown, layout)
            if x <= x1 and x + layout.card_w >= x0 and y - layout.caption <= y1 and y + layout.card_h >= y0]


def _rectangle(shader, rect, color):
    x, y, w, h = rect
    if w <= 0 or h <= 0:
        return
    verts = ((x, y), (x+w, y), (x+w, y+h), (x, y+h))
    batch = batch_for_shader(shader, 'TRI_FAN', {"pos": verts})
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


def _scroll_shape(x, y, width, height, roundness, color):
    if min(width, height, color[3]) <= 0:
        return
    radius = min(width, height) * .5 * roundness
    vertices = []
    for i, (cx, cy) in enumerate(((x+width-radius, y+height-radius), (x+radius, y+height-radius),
                                   (x+radius, y+radius), (x+width-radius, y+radius))):
        for step in range(7):
            angle = (i + step / 6) * pi / 2
            vertices.append((cx + radius * cos(angle), cy + radius * sin(angle)))
    batch = batch_for_shader(_solid, 'TRI_FAN', {"pos": vertices})
    _solid.bind()
    _solid.uniform_float("color", color)
    batch.draw(_solid)


def _draw_scrollbar(layout, theme):
    key = _key(bpy.context)
    if not layout.scroll_width:
        _scroll_grids.pop(key, None)
        _scroll_proximity.pop(key, None)
        return
    _scroll_grids[key] = layout
    pressed = key in _pressed_bars
    proximity = 1.0 if pressed else _scroll_proximity.get(key, 0.0)
    width = round(layout.idle_width + (layout.scroll_width - layout.idle_width) * proximity)
    x = layout.scroll_left + layout.scroll_width - width
    opacity = .4 + .6 * proximity
    inner = theme.inner
    alpha = inner[3] * opacity if inner[3] else .25 * proximity
    inset = layout.track_inset
    _scroll_shape(x, inset, width, layout.height - 2 * inset, theme.roundness, (*inner[:3], alpha))
    bottom, size = layout.thumb
    item = theme.item
    rgb = tuple(min(1, c + 5 / 255) for c in item[:3]) if pressed else item[:3]
    _scroll_shape(x, bottom, width, size, theme.roundness, (*rgb, item[3] * opacity))


def _label(label, x, y, size, color=(.88, .89, .91, 1), width=None):
    blf.size(0, max(8, round(size)))
    if width is not None and blf.dimensions(0, label)[0] > width:
        while label and blf.dimensions(0, label + "…")[0] > width:
            label = label[:-1]
        label += "…"
    blf.color(0, *color)
    blf.position(0, x, y, 0)
    blf.draw(0, label)


def _texture(photo):
    preview = catalog.thumbnail_preview(photo)
    if not preview:
        return None
    w, h = preview.image_size
    if w < 1 or h < 1:
        return None
    key = (photo.uid, photo.thumbnail_path, preview.icon_id, w, h)
    cached = _textures.get(key)
    if cached:
        _textures.move_to_end(key)
        return cached
    pixels = array('f', preview.image_pixels_float[:])
    if len(pixels) != w * h * 4:
        return None
    buf = gpu.types.Buffer('FLOAT', len(pixels), pixels)
    texture = gpu.types.GPUTexture((w, h), format='RGBA16F', data=buf)
    _textures[key] = (texture, w, h)
    while len(_textures) > 96:
        _textures.popitem(last=False)
    return _textures[key]


def _thumbnail(photo, x, y, w, h):
    if min(w, h) <= 0:
        return
    packed = _texture(photo)
    if not packed:
        return
    texture, iw, ih = packed
    fit = min(w / iw, h / ih)
    width, height = iw * fit, ih * fit
    x, y = x + (w-width)*.5, y + (h-height)*.5
    vertices = ((x, y), (x+width, y), (x+width, y+height), (x, y+height))
    coords = ((0, 0), (1, 0), (1, 1), (0, 1))
    batch = batch_for_shader(_image_shader, 'TRI_FAN', {"pos": vertices, "texCoord": coords})
    _image_shader.bind()
    _image_shader.uniform_sampler("image", texture)
    batch.draw(_image_shader)


def _gallery_header(header, context):
    if (context.space_data.show_region_ui and context.space_data.as_pointer() in _new_sidebars
            and not bpy.app.timers.is_registered(_select_initial_sidebar)):
        bpy.app.timers.register(_select_initial_sidebar, first_interval=.08)
    layout = header.layout
    lib = catalog.library(context)
    layout.template_header()
    layout.operator_context = 'INVOKE_DEFAULT'
    available = context.area.width / max(.75, context.preferences.system.ui_scale)
    row = layout.row(align=True)
    compact = available < 520
    row.operator("blender_raw.import_photos", text="" if compact else "Import", icon='IMPORT')
    row.operator("blender_raw.export_photos", text="" if compact else "Export", icon='EXPORT')
    layout.separator_spacer()
    row = layout.row(align=True)
    row.operator("blender_raw.gallery_filter", text="" if compact else "Filter", icon='FILTER')
    if available < 170:
        row.popover(panel=BR_PT_gallery_display.bl_idname, text="", icon='OPTIONS')
    else:
        preview = row.row(align=True)
        preview.ui_units_x = 3 if compact else 7.5
        preview.prop(lib, "preview_resolution", text="" if compact else "Preview")
        size = row.row(align=True)
        size.ui_units_x = 2 if compact else 4.5
        size.prop(lib, "thumbnail_scale", text="" if compact else "Size")


def _switch_region_defaults(space):
    # Node Editor subtypes share a SpaceNodeEditor. Swap their side-region
    # preferences only when the editor changes, leaving Blender's T/N controls live.
    key = space.as_pointer()
    previous_mode = _region_modes.get(key)
    if space.tree_type == previous_mode:
        return
    visible = (space.show_region_toolbar, space.show_region_ui)
    if previous_mode == GALLERY_TYPE:
        _gallery_regions[key] = visible
    _region_modes[key] = space.tree_type
    if space.tree_type == GALLERY_TYPE:
        if key not in _ordinary_regions:
            _ordinary_regions[key] = visible
        if key not in _gallery_regions:
            _new_sidebars.add(key)
        space.show_region_toolbar, space.show_region_ui = _gallery_regions.pop(key, (False, False))
    elif key in _ordinary_regions:
        previous = _ordinary_regions.pop(key)
        if previous is not None:
            space.show_region_toolbar, space.show_region_ui = previous


def _select_initial_sidebar():
    # Blender builds category tabs when N first opens the region. Select our tab
    # once for newly created Galleries; later choices belong to the user.
    waiting = False
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            space = area.spaces.active
            if area.ui_type != GALLERY_TYPE or not space.show_region_ui or space.as_pointer() not in _new_sidebars:
                continue
            region = next((r for r in area.regions if r.type == 'UI'), None)
            if region and region.width > 1 and region.active_panel_category:
                try:
                    region.active_panel_category = GALLERY_NAME
                    _new_sidebars.discard(space.as_pointer())
                    continue
                except (RuntimeError, TypeError):
                    pass  # Category names become available after the first sidebar draw.
            waiting = True
    return .1 if waiting else None


def _on_editor_change():
    # Region changes need to run outside a header draw callback.
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'NODE_EDITOR':
                _switch_region_defaults(area.spaces.active)


@persistent
def _use_saved_region_visibility(*_):
    # A reopened Gallery already carries its user's saved T/N choices.
    for screen in bpy.data.screens:
        for area in screen.areas:
            for space in area.spaces:
                if space.type == 'NODE_EDITOR' and space.tree_type == GALLERY_TYPE:
                    _ordinary_regions.setdefault(space.as_pointer(), None)
                    _region_modes[space.as_pointer()] = GALLERY_TYPE


def _install_header():
    # Header types have no poll callback in Blender 5.3. Route only our subtype
    # here and keep the normal draw chain, including other add-ons, everywhere else.
    global _header_override
    header = bpy.types.NODE_HT_header
    header.append(_gallery_header)
    header.remove(_gallery_header)
    original = header.draw

    @wraps(original)
    def draw(self, context):
        is_gallery = _is_gallery(context)
        if context.space_data.tree_type != _region_modes.get(context.space_data.as_pointer()):
            if not bpy.app.timers.is_registered(_on_editor_change):
                bpy.app.timers.register(_on_editor_change)
        if is_gallery:
            return _gallery_header(self, context)
        return original(self, context)

    # wraps preserves Blender's shared callback list for later append/remove.
    header.draw = draw
    _header_override = (original, draw)


def _restore_header():
    global _header_override
    if _header_override:
        original, replacement = _header_override
        if bpy.types.NODE_HT_header.draw is replacement:
            bpy.types.NODE_HT_header.draw = original
        _header_override = None


def _draw_box(rect, scale):
    x, y, right, top = rect
    w, h = right - x, top - y
    edge = max(1, round(scale))
    _rectangle(_solid, (x, y, w, h), (.32, .60, .93, .17))
    for side in ((x, y, w, edge), (x, top-edge, w, edge),
                 (x, y, edge, h), (right-edge, y, edge, h)):
        _rectangle(_solid, side, (.70, .84, 1, .95))


def _draw_canvas():
    context = bpy.context
    if not _is_gallery(context) or not context.region or context.region.type != 'WINDOW':
        return
    global _solid, _image_shader
    if _solid is None:
        _solid = gpu.shader.from_builtin('UNIFORM_COLOR')
        _image_shader = gpu.shader.from_builtin('IMAGE')
    library = catalog.library(context)
    shown = _filtered(library)
    layout = _layout(context, shown)
    width, height, s, pad, cw, ch = layout[:6]
    displayed_uid = context.scene.get(compositor.PHOTO_TAG) if context.scene.get(compositor.STUDIO_TAG) else None
    previous_blend = gpu.state.blend_get()
    try:
        gpu.state.blend_set('ALPHA')
        _rectangle(_solid, (0, 0, context.region.width, height), (.065, .07, .075, 1))
        for _, photo, x, y in _visible_cards(shown, layout):
            if photo.uid == displayed_uid:
                border = (.28, .76, .42, 1)
            elif photo.selected:
                border = (.28, .58, .84, 1)
            else:
                border = (.15, .16, .17, 1)
            _rectangle(_solid, (x,y,cw,ch), border)
            _rectangle(_solid, (x+2,y+2,cw-4,ch-4), (.055,.058,.061,1))
            _thumbnail(photo, x+2,y+2,cw-4,ch-4)
            rating = str(photo.rating) + "★" if photo.rating else ""
            baseline = y - round(15*s)
            _label(photo.name, x+2*s, baseline, 12*s, width=cw-(28*s if rating else 4*s))
            if rating:
                _label(rating, x+cw-round(22*s), baseline, 11*s, (.89,.72,.42,1))
            if photo.selected:
                _rectangle(_solid, (x+5*s, y+ch-14*s, 9*s, 9*s), (.34,.65,.89,1))
            if photo.flagged:
                fx, fy = x + cw - 24*s, y + ch - 22*s
                _rectangle(_solid, (fx-2*s, fy-2*s, 21*s, 21*s), (.06,.07,.08,.85))
                _rectangle(_solid, (fx+3*s, fy+2*s, 2*s, 14*s), (1,.87,.49,1))
                _rectangle(_solid, (fx+5*s, fy+9*s, 9*s, 7*s), (1,.87,.49,1))
        if not shown:
            message = "Import Photos to begin" if not library.photos else "No photos match the filters"
            _label(message, layout.left+pad*2, height-32*s, 14*s, width=width-pad*3)
        # BLF resets GPU blending after drawing filenames and captions.
        gpu.state.blend_set('ALPHA')
        box = _boxes.get(_key(context))
        if box:
            _draw_box(box, s)
        _draw_scrollbar(layout, context.preferences.themes[0].user_interface.wcol_scroll)
    finally:
        gpu.state.blend_set(previous_blend)


def _redraw(context):
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'NODE_EDITOR':
                area.tag_redraw()


def _size_preference_changed(self, context):
    if context.window_manager:
        _redraw(context)


class BR_GalleryPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    from .ofx_config import changed as _ofx_path_changed
    spektrafilm_ofx_path: StringProperty(
        name="Spektrafilm OFX Folder", subtype='DIR_PATH',
        description="Complete official plugin folder; leave blank to use the system OFX installation",
        update=_ofx_path_changed)

    thumbnail_s: IntProperty(name="Small (S)", description="Maximum thumbnail width in UI pixels",
                             default=catalog.THUMBNAIL_DEFAULTS["S"], min=50, soft_max=800, update=_size_preference_changed)
    thumbnail_m: IntProperty(name="Medium (M)", description="Maximum thumbnail width in UI pixels",
                             default=catalog.THUMBNAIL_DEFAULTS["M"], min=50, soft_max=800, update=_size_preference_changed)
    thumbnail_l: IntProperty(name="Large (L)", description="Maximum thumbnail width in UI pixels",
                             default=catalog.THUMBNAIL_DEFAULTS["L"], min=50, soft_max=800, update=_size_preference_changed)

    def draw(self, context):
        self.layout.label(text="Gallery thumbnail widths (UI pixels)")
        column = self.layout.column()
        column.use_property_split = True
        for name in ("thumbnail_s", "thumbnail_m", "thumbnail_l"):
            column.prop(self, name)
        self.layout.separator()
        self.layout.label(text="Spektrafilm OFX")
        self.layout.prop(self, 'spektrafilm_ofx_path')
        self.layout.operator('blender_raw.locate_spektrafilm_ofx', icon='FILE_FOLDER')
        self.layout.label(text="Uses the complete official plugin. Lite is included with the extension.")


def _activate(context, index, photo, extend=False, toggle=False):
    library = catalog.library(context)
    visible = _filtered(library)
    ids = [entry[0] for entry in visible]
    if extend and ids:
        anchor_uid = _anchors.get(_key(context))
        anchor = next((i for i, item in visible if item.uid == anchor_uid), library.active_index)
        a = ids.index(anchor) if anchor in ids else ids.index(index)
        b = ids.index(index)
        selected = set(ids[min(a,b):max(a,b)+1])
        for i, item in enumerate(library.photos):
            item.selected = i in selected
    elif toggle:
        photo.selected = not photo.selected
        _anchors[_key(context)] = photo.uid
    else:
        for i, item in enumerate(library.photos):
            item.selected = i == index
        _anchors[_key(context)] = photo.uid
    if not toggle and not extend:
        bpy.ops.blender_raw.open_photo(uid=photo.uid)
    _redraw(context)


class BR_OT_gallery_click(bpy.types.Operator):
    bl_idname = "blender_raw.gallery_click"
    bl_label = "Select Gallery Photo"
    bl_options = {'INTERNAL'}
    wait_for_input: BoolProperty(options={'SKIP_SAVE'}, default=False)

    @classmethod
    def poll(cls, context):
        return _is_gallery(context) and context.region and context.region.type == 'WINDOW'

    def invoke(self, context, event):
        self._start = None
        self._box = self.wait_for_input
        self._key = _key(context)
        if self.wait_for_input:
            context.window.cursor_modal_set('CROSSHAIR')
        else:
            self._begin(context, event)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _begin(self, context, event):
        self._start = (event.mouse_region_x, event.mouse_region_y)
        self._extend = event.shift
        self._subtract = event.ctrl or event.oskey

    def _bounds(self, event):
        x0, y0 = self._start
        x1, y1 = event.mouse_region_x, event.mouse_region_y
        return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)

    def _finish(self, context):
        _boxes.pop(self._key, None)
        if self.wait_for_input:
            context.window.cursor_modal_restore()
        if context.area:
            context.area.tag_redraw()

    def cancel(self, context):
        self._finish(context)

    def modal(self, context, event):
        if (event.type in {'ESC', 'RIGHTMOUSE', 'WINDOW_DEACTIVATE'} or
                not _is_gallery(context) or _key(context) != self._key):
            self._finish(context)
            return {'CANCELLED'}
        if self._start is None:
            if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                self._begin(context, event)
            return {'RUNNING_MODAL'}
        if event.type not in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE', 'LEFTMOUSE'}:
            return {'RUNNING_MODAL'}
        box = self._bounds(event)
        x0, y0, x1, y1 = box
        if max(x1-x0, y1-y0) >= max(2, context.preferences.inputs.drag_threshold_mouse):
            self._box = True
        if self._box:
            _boxes[self._key] = box
            context.area.tag_redraw()
        if event.type != 'LEFTMOUSE' or event.value != 'RELEASE':
            return {'RUNNING_MODAL'}
        lib = catalog.library(context)
        hits = _hit_cards(context, box)
        self._finish(context)
        if not self._box and hits:
            _activate(context, *hits[0], extend=self._extend, toggle=self._subtract)
            return {'FINISHED'}
        if not (self._extend or self._subtract):
            for photo in lib.photos:
                photo.selected = False
        for _, photo in hits:
            photo.selected = not self._subtract
        _redraw(context)
        return {'FINISHED'}


class BR_OT_gallery_navigate(bpy.types.Operator):
    bl_idname = "blender_raw.gallery_navigate"
    bl_label = "Navigate Gallery"
    bl_options = {'INTERNAL'}
    action: EnumProperty(items=[(key, key.title(), "") for key in
                                ('SCROLL_UP','SCROLL_DOWN','PAN','LEFT','RIGHT','UP','DOWN','ALL','NONE')])

    @classmethod
    def poll(cls, context):
        return _is_gallery(context) and context.region and context.region.type == 'WINDOW'

    def invoke(self, context, event):
        if self.action != 'PAN':
            return self.execute(context)
        grid = _layout(context, _filtered(catalog.library(context)))
        # View2D moves its viewport by prev_y - y. Our offset moves the content
        # in the opposite direction. Cocoa already supplied device-scaled scroll
        # deltas with the user's natural/traditional direction applied.
        delta = event.mouse_y - event.mouse_prev_y
        _offsets[_key(context)] = max(0, min(grid.maximum, grid.offset + delta))
        context.area.tag_redraw()
        return {'FINISHED'}

    def execute(self, context):
        lib = catalog.library(context)
        shown = _filtered(lib)
        layout = _layout(context, shown)
        key = _key(context)
        if self.action in {'ALL','NONE'}:
            if self.action == 'NONE':
                for photo in lib.photos:
                    photo.selected = False
            else:
                for _,photo in shown:
                    photo.selected = True
        elif self.action in {'SCROLL_UP', 'SCROLL_DOWN'}:
            step = min(layout.row_step, layout.height)
            delta = step if self.action == 'SCROLL_DOWN' else -step
            _offsets[key] = max(0, min(layout.maximum, layout.offset + delta))
            context.area.tag_redraw()
            return {'FINISHED'}
        elif shown and self.action != 'PAN':
            delta = {'LEFT': -1, 'RIGHT': 1, 'UP': -layout.columns, 'DOWN': layout.columns}[self.action]
            ids = [index for index,_ in shown]
            current = ids.index(lib.active_index) if lib.active_index in ids else None
            pos = max(0, min(len(shown)-1, current+delta)) if current is not None else 0
            _offsets[key] = _reveal_row(layout, pos)
            _activate(context, *shown[pos])
        _redraw(context)
        return {'FINISHED'}


class BR_OT_gallery_scrollbar(bpy.types.Operator):
    bl_idname = "blender_raw.gallery_scrollbar"
    bl_label = "Scroll Photo Gallery"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return (bool(_scroll_proximity and context.window != _hover_window)
                or (_is_gallery(context) and context.region and context.region.type == 'WINDOW'))

    def _move_thumb(self, grid, y):
        _, size = grid.thumb
        travel = max(1, grid.height - 2 * grid.track_inset - size)
        bottom = y - self._grab * size - grid.track_inset
        offset = round((1 - bottom / travel) * grid.maximum)
        _offsets[self._area] = max(0, min(grid.maximum, offset))

    def _set_proximity(self, context, value):
        global _hover_window
        if value == _scroll_proximity.get(self._area, 0.0):
            return
        if value:
            _hover_window = context.window
            _scroll_proximity[self._area] = value
        else:
            _scroll_proximity.pop(self._area, None)
            if not _scroll_proximity:
                _hover_window = None
        if context.area and _key(context) == self._area:
            context.area.tag_redraw()

    def _press(self, context, event, grid):
        self._initial = grid.offset
        self._button = event.type
        bottom, size = grid.thumb
        y = event.mouse_region_y
        if event.type == 'MIDDLEMOUSE':
            self._grab = .5
            self._move_thumb(grid, y)
        elif bottom <= y < bottom + size:
            self._grab = (y - bottom) / size
        else:
            self._grab = None
            # Blender's View2D track click leaves 20% of the old page in view.
            self._page = round(grid.height * .8) * (1 if y < bottom else -1)
        _pressed_bars.add(self._area)
        context.area.tag_redraw()

    def invoke(self, context, event):
        global _hover_window
        if event.type == 'MOUSEMOVE' and _scroll_proximity and context.window != _hover_window:
            # Mouse motion in a second window need not deactivate the first.
            # Its modal handler cannot receive these events.
            for window in context.window_manager.windows:
                if window == _hover_window:
                    for area in window.screen.areas:
                        if area.as_pointer() in _scroll_proximity:
                            area.tag_redraw()
            _scroll_proximity.clear()
            _hover_window = None
        if not _is_gallery(context) or not context.region or context.region.type != 'WINDOW':
            return {'PASS_THROUGH'}
        self._area = _key(context)
        grid = _scroll_grids.get(self._area)
        if not grid or self._area in _scroll_proximity:
            return {'PASS_THROUGH'}
        x, y = event.mouse_region_x, event.mouse_region_y
        proximity = grid.proximity(x, y)
        if not proximity:
            return {'PASS_THROUGH'}
        self._button = None
        if event.type != 'MOUSEMOVE':
            if not grid.scroll_left <= x < grid.scroll_left + grid.scroll_width:
                return {'PASS_THROUGH'}
            self._press(context, event, grid)
        self._set_proximity(context, proximity)
        # Follow the pointer across the region boundary only while it is close
        # to the bar. Unrelated events still reach the region under the mouse.
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'} if self._button else {'RUNNING_MODAL', 'PASS_THROUGH'}

    def cancel(self, context):
        _pressed_bars.discard(self._area)
        if self._button and self._area in _scroll_grids:
            _offsets[self._area] = self._initial
        self._set_proximity(context, 0.0)
        if context.area and _key(context) == self._area:
            context.area.tag_redraw()

    def modal(self, context, event):
        if not self._button and self._area not in _scroll_proximity:
            return {'FINISHED', 'PASS_THROUGH'}
        grid = _scroll_grids.get(self._area)
        if (not grid or not _is_gallery(context) or _key(context) != self._area
                or event.type == 'WINDOW_DEACTIVATE'):
            self.cancel(context)
            return {'CANCELLED', 'PASS_THROUGH'}
        x, y = event.mouse_region_x, event.mouse_region_y
        if not self._button:
            proximity = grid.proximity(x, y)
            self._set_proximity(context, proximity)
            if not proximity:
                return {'FINISHED', 'PASS_THROUGH'}
            if (event.type in {'LEFTMOUSE', 'MIDDLEMOUSE'} and event.value == 'PRESS'
                    and grid.scroll_left <= x < grid.scroll_left + grid.scroll_width):
                self._press(context, event, grid)
                return {'RUNNING_MODAL'}
            return {'PASS_THROUGH'}
        if event.type in {'ESC', 'RIGHTMOUSE'}:
            self.cancel(context)
            return {'CANCELLED'}
        if event.type == self._button and event.value == 'RELEASE':
            if self._grab is None:
                _offsets[self._area] = max(0, min(grid.maximum, grid.offset + self._page))
            _pressed_bars.discard(self._area)
            self._button = None
            proximity = grid.proximity(x, y)
            self._set_proximity(context, proximity)
            context.area.tag_redraw()
            return {'RUNNING_MODAL'} if proximity else {'FINISHED'}
        if self._grab is not None and event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            self._move_thumb(grid, y)
            context.area.tag_redraw()
        return {'RUNNING_MODAL'}


def _filter_ui(layout, lib):
    layout.prop(lib, "query", text="Search", icon='VIEWZOOM')
    layout.prop(lib, "min_rating", text="Minimum Rating")
    layout.prop(lib, "flag_filter", text="Flags")
    filters.draw(layout, lib)


def _selection(context):
    return [p for p in catalog.library(context).photos if p.selected]


def _has_selection(context):
    return _is_gallery(context) and any(p.selected for p in catalog.library(context).photos)


class BR_OT_rate_photos(bpy.types.Operator):
    bl_idname = "blender_raw.rate_photos"
    bl_label = "Set Photo Rating"
    bl_description = "Apply this rating to every selected Gallery photo"
    bl_options = {'REGISTER', 'UNDO'}
    rating: IntProperty(name="Rating", min=0, max=5)

    @classmethod
    def poll(cls, context):
        return _has_selection(context)

    def execute(self, context):
        for photo in _selection(context):
            photo.rating = self.rating
        _redraw(context)
        return {'FINISHED'}


class BR_OT_flag_photos(bpy.types.Operator):
    bl_idname = "blender_raw.flag_photos"
    bl_label = "Set Photo Flags"
    bl_description = "Flag or unflag the selected Gallery photos"
    bl_options = {'REGISTER', 'UNDO'}
    flagged: BoolProperty(name="Flagged", default=True)

    @classmethod
    def poll(cls, context):
        return _has_selection(context)

    def execute(self, context):
        for photo in _selection(context):
            photo.flagged = self.flagged
        _redraw(context)
        return {'FINISHED'}


def _rating_actions(layout, selected, *, compact):
    ratings = {p.rating for p in selected}
    row = layout.row(align=True) if compact else layout
    labels = ("Clear Rating", "1 Star", "2 Stars", "3 Stars", "4 Stars", "5 Stars")
    for rating, label in enumerate(labels):
        checked = ratings == {rating}
        icon = 'NONE' if compact else ('CHECKMARK' if checked else 'BLANK1')
        row.operator("blender_raw.rate_photos", text=str(rating) if compact else label,
                     icon=icon, depress=checked).rating = rating


def _flag_actions(layout, selected, *, compact):
    flags = {p.flagged for p in selected}
    row = layout.row(align=True) if compact else layout
    for value, label in ((True, "Flag"), (False, "Unflag")):
        checked = flags == {value}
        icon = 'NONE' if compact else ('CHECKMARK' if checked else 'BLANK1')
        row.operator("blender_raw.flag_photos", text=label, icon=icon, depress=checked).flagged = value


class BR_OT_copy_adjustments(bpy.types.Operator):
    bl_idname = "blender_raw.copy_adjustments"
    bl_label = "Copy Adjustments"
    bl_description = "Copy this photo’s processing nodes starting after its Raw Reader"
    bl_options = {'INTERNAL'}
    uid: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        photo = catalog.lookup_photo(context.scene, self.uid)
        if not photo:
            return {'CANCELLED'}
        try:
            adjustments.copy(context, photo)
        except (ValueError, RuntimeError, OSError) as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, f"Copied adjustments from {photo.name}")
        return {'FINISHED'}


class BR_OT_paste_adjustments(bpy.types.Operator):
    bl_idname = "blender_raw.paste_adjustments"
    bl_label = "Paste Adjustments"
    bl_description = "Replace the selected photos’ adjustments and keep each photo’s own Raw Reader"
    bl_options = {'INTERNAL', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return adjustments.has_targets(_selection(context))

    def execute(self, context):
        try:
            count = adjustments.paste(context, _selection(context))
        except (ValueError, RuntimeError, OSError) as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        _redraw(context)
        self.report({'INFO'}, f"Pasted adjustments to {count} photo{'s' if count != 1 else ''}")
        return {'FINISHED'}


class BR_MT_gallery_photo(bpy.types.Menu):
    bl_idname = "BLENDERRAW_MT_gallery_photo"
    bl_label = "Photo Gallery Selection"

    def draw(self, context):
        layout = self.layout
        layout.operator_context = 'INVOKE_DEFAULT'
        _photo_ui(layout, context, menu=True)
        layout.operator("blender_raw.pack_selected", text="Embed Selected Originals", icon='PACKAGE')
        layout.separator()
        _selection_actions(layout, menu=True)
        _export_ui(layout)


class BR_OT_gallery_context(bpy.types.Operator):
    bl_idname = "blender_raw.gallery_context"
    bl_label = "Gallery Photo Menu"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return _is_gallery(context) and context.region and context.region.type == 'WINDOW'

    def invoke(self, context, event):
        x, y = event.mouse_region_x, event.mouse_region_y
        hits = _hit_cards(context, (x, y, x, y))
        _context_photos[_key(context)] = hits[0][1].uid if hits else None
        if hits:
            index, photo = hits[0]
            if not photo.selected:
                lib = catalog.library(context)
                for i, item in enumerate(lib.photos):
                    item.selected = i == index
                _anchors[_key(context)] = photo.uid
                _redraw(context)
        bpy.ops.wm.call_menu(name=BR_MT_gallery_photo.bl_idname)
        return {'FINISHED'}


def _selection_actions(layout, *, menu=False):
    select = layout.column() if menu else layout.row(align=True)
    select.operator_context = 'EXEC_REGION_WIN'
    select.operator("blender_raw.gallery_navigate", text="Select All").action = 'ALL'
    select.operator("blender_raw.gallery_navigate", text="Select None").action = 'NONE'


class BR_OT_edit_gallery_tags(bpy.types.Operator):
    bl_idname = "blender_raw.edit_gallery_tags"
    bl_label = "Edit Tags"
    bl_options = {'REGISTER', 'UNDO'}
    uid: StringProperty(options={'HIDDEN'})
    tags: StringProperty(name="Tags", description="Separate tags with commas")

    def invoke(self, context, event):
        photo = catalog.lookup_photo(context.scene, self.uid)
        if not photo:
            return {'CANCELLED'}
        self.tags = photo.tags
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        photo = catalog.lookup_photo(context.scene, self.uid)
        if not photo:
            return {'CANCELLED'}
        photo.tags = self.tags
        _redraw(context)
        return {'FINISHED'}


def _photo_ui(layout, context, *, menu=False):
    selected = _selection(context)
    if not selected:
        layout.label(text="Select a photo in the Gallery")
        return
    title = selected[0].name if len(selected) == 1 else f"{len(selected)} Photos Selected"
    layout.label(text=title, icon='IMAGE_DATA')
    layout.label(text="Rating")
    _rating_actions(layout, selected, compact=not menu)
    if menu:
        layout.separator()
    _flag_actions(layout, selected, compact=not menu)
    if menu:
        layout.separator()
        uid = _context_photos.get(_key(context))
        source = next((p for p in selected if p.uid == uid), None)
        if not source and len(selected) == 1:
            source = selected[0]
        row = layout.row()
        row.enabled = source is not None
        row.operator("blender_raw.copy_adjustments", icon='COPYDOWN').uid = source.uid if source else ""
        layout.operator("blender_raw.paste_adjustments", icon='PASTEDOWN')
        layout.separator()
    layout.operator("blender_raw.remove_photo", text="Remove from Gallery", icon='X')
    if len(selected) != 1:
        return
    photo = selected[0]
    if menu:
        layout.separator()
        layout.operator("blender_raw.edit_gallery_tags", text="Edit Tags…").uid = photo.uid
    else:
        layout.prop(photo, "tags", text="Tags")
    layout.label(text="Embedded Original" if photo.packed else "Linked Original",
                 icon='PACKAGE' if photo.packed else 'LINKED')
    locate = layout.column()
    locate.enabled = not photo.packed
    locate.operator("blender_raw.relink_photo", text="Locate Original", icon='FILE_FOLDER').uid = photo.uid
    if photo.error:
        layout.label(text=photo.error, icon='ERROR')


def redraw_statusbars():
    # Global status bars aren't included in Screen.areas. Keep the live header
    # region supplied by Blender, including those in secondary windows.
    live_windows = {w.as_pointer() for w in bpy.context.window_manager.windows}
    for key, region in list(_status_regions.items()):
        if key not in live_windows:
            _status_regions.pop(key)
            continue
        try:
            region.tag_redraw()
        except ReferenceError:
            _status_regions.pop(key, None)


def _statusbar_export(header, context):
    from . import jobs
    if context.window and context.region:
        _status_regions[context.window.as_pointer()] = context.region
    progress = jobs.export_progress()
    if progress is None:
        return
    completed, total = progress
    row = header.layout.row(align=True)
    bar = row.row()
    bar.ui_units_x = 9
    bar.progress(factor=completed / total if total else 0.0, type='BAR', text=f"Photo Export {completed}/{total}")
    row.operator("blender_raw.cancel_export", text="", icon='CANCEL')
    if context.region.width >= 900 * context.preferences.system.ui_scale and (activity := jobs.export_activity()):
        row.label(text=activity)


def _export_ui(layout):
    from . import jobs
    if jobs.export_running():
        return
    status = jobs.export_status()
    if status:
        layout.label(text=status, icon='INFO')
    for item in jobs.export_details():
        layout.label(text=item)


class BR_OT_gallery_filter(bpy.types.Operator):
    bl_idname = "blender_raw.gallery_filter"
    bl_label = "Filter Photos"

    def invoke(self,context,event):
        return context.window_manager.invoke_popup(self,width=280)

    def draw(self,context):
        _filter_ui(self.layout,catalog.library(context))

    def execute(self,context):
        return {'FINISHED'}


class BR_OT_gallery_details(bpy.types.Operator):
    bl_idname = "blender_raw.gallery_details"
    bl_label = "Photo Details"

    def invoke(self,context,event):
        return context.window_manager.invoke_popup(self,width=300)

    def draw(self,context):
        layout = self.layout
        lib = catalog.library(context)
        count = sum(1 for photo in lib.photos if photo.selected)
        layout.label(text=f"{count} selected / {len(lib.photos)} photos")
        _selection_actions(layout)
        layout.separator()
        _photo_ui(layout, context)
        layout.operator("blender_raw.pack_selected", text="Embed Selected Originals", icon='PACKAGE')
        _export_ui(layout)

    def execute(self,context):
        return {'FINISHED'}


class BR_PT_gallery_display(bpy.types.Panel):
    bl_idname = "BLENDERRAW_PT_gallery_display"
    bl_label = "Gallery Display"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'HEADER'

    @classmethod
    def poll(cls, context):
        return _is_gallery(context)

    def draw(self, context):
        lib = catalog.library(context)
        self.layout.prop(lib, "preview_resolution")
        self.layout.prop(lib, "thumbnail_scale")


class BR_PT_gallery_sidebar(bpy.types.Panel):
    bl_idname = "BLENDERRAW_PT_gallery_sidebar"
    bl_label = GALLERY_NAME
    bl_category = GALLERY_NAME
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_order = -100
    bl_options = {'HIDE_HEADER'}

    @classmethod
    def poll(cls, context):
        return _is_gallery(context)

    def draw(self, context):
        from . import jobs
        lib = catalog.library(context)
        layout = self.layout
        layout.operator_context = 'INVOKE_DEFAULT'
        row = layout.row(align=True)
        row.operator("blender_raw.import_photos", text="Import", icon='IMPORT')
        row.operator("blender_raw.export_photos", text="Export", icon='EXPORT')

        heading, body = layout.panel("photo_gallery_display")
        heading.label(text="Display and Cache")
        if body:
            body.prop(lib, "thumbnail_scale")
            body.prop(lib, "preview_resolution")
            body.prop(lib, "pack_cache_images")
            if bpy.data.use_autopack:
                body.label(text="Blender Auto Pack is on", icon='INFO')
            elif not lib.pack_cache_images and catalog.has_packed_cache_images():
                body.operator("blender_raw.externalize_cache_images")
            if lib.cache_storage_error:
                body.label(text=lib.cache_storage_error, icon='ERROR')

        heading, body = layout.panel("photo_gallery_selection")
        heading.label(text="Selection")
        if body:
            body.label(text=f"{len(_selection(context))} selected / {len(lib.photos)} photos")
            _selection_actions(body)
            _photo_ui(body, context)
            body.operator("blender_raw.pack_selected", text="Embed Selected Originals", icon='PACKAGE')

        heading, body = layout.panel("photo_gallery_filters", default_closed=True)
        heading.label(text="Filters")
        if body:
            _filter_ui(body, lib)

        heading, body = layout.panel("photo_gallery_export", default_closed=True)
        heading.label(text="Export Settings")
        if body:
            body.prop(lib, "rename_exports")
            body.prop(lib, "overwrite_exports")
            body.template_image_settings(context.scene.render.image_settings, color_management=True)
        if jobs.export_running():
            layout.operator("blender_raw.cancel_export", text="Cancel Export", icon='CANCEL')
        _export_ui(layout)


def _gallery_area(area):
    area.type = 'NODE_EDITOR'
    area.ui_type = GALLERY_TYPE
    _switch_region_defaults(area.spaces.active)
    area.tag_redraw()


class BR_OT_open_gallery(bpy.types.Operator):
    bl_idname = "blender_raw.open_gallery"
    bl_label = "Open Photo Gallery"
    bl_description = "Show this project's Photo Gallery in the current area"

    def execute(self, context):
        _gallery_area(context.area)
        return {'FINISHED'}


def _split(window, area, direction, factor):
    with bpy.context.temp_override(window=window, area=area):
        result = bpy.ops.screen.area_split(direction=direction, factor=factor)
    if 'FINISHED' not in result:
        raise RuntimeError("Unable to split the photo workspace")


def _append_workspace_template(window):
    # A native Workspace asset preserves panel folds and draggable graph heights
    # that Blender 5.3 does not expose in RNA. Only new workspaces use this seed.
    path = Path(__file__).with_name('assets') / 'photo_workspace.blend'
    with bpy.data.libraries.load(str(path), link=False) as (source, target):
        if 'Photo Editor Scopes' not in source.workspaces:
            raise RuntimeError("Photo Workspace template is missing")
        target.workspaces = ['Photo Editor Scopes']
    workspace = target.workspaces[0]
    workspace.use_fake_user = False
    window.workspace = workspace


def _gallery_fraction(width, scale, thumbnail_limit):
    pad = round(8 * scale)
    side = round(thumbnail_limit * scale)
    # Allow for the split border. The scrollbar overlays the card grid.
    target = side + 2 * pad + round(5 * scale)
    viewer_min = round(400 * scale)
    gallery_width = min(target, max(width * .25, width - viewer_min))
    return min(.8, gallery_width / width)


def finish_workspace_panels(window):
    """Choose default sidebar tabs once their native panels are available."""
    workspace = window.workspace
    key = workspace.as_pointer()
    if not workspace.get("photo_editor_pending_tabs") or key in _panel_timers:
        return
    attempts = 0

    def select_tabs():
        nonlocal attempts
        try:
            if window.workspace == workspace:
                pending = set(workspace.get("photo_editor_pending_tabs", "").split())
                for area in window.screen.areas:
                    space = area.spaces.active
                    if area.type == 'IMAGE_EDITOR' and 'Scopes' in pending:
                        if not space.image:
                            continue
                        tab = 'Scopes'
                    elif area.ui_type == 'CompositorNodeTree' and 'Node' in pending:
                        if not space.edit_tree or not space.edit_tree.nodes.active:
                            continue
                        tab = 'Node'
                    else:
                        continue
                    region = next((r for r in area.regions if r.type == 'UI'), None)
                    if region and region.width > 1 and region.active_panel_category is not None:
                        try:
                            region.active_panel_category = tab
                        except (RuntimeError, TypeError):
                            continue  # Blender builds the category list during the first draw.
                        if region.active_panel_category == tab:
                            pending.remove(tab)
                workspace["photo_editor_pending_tabs"] = " ".join(sorted(pending))
                attempts += 1
                if pending and attempts < 12:
                    return .12
        except ReferenceError:
            pass
        _panel_timers.pop(key, None)
        return None

    _panel_timers[key] = select_tabs
    bpy.app.timers.register(select_tabs, first_interval=.12)


def viewer_column(screen):
    # Refuse to guess between multiple image editors in a customized layout.
    images = [a for a in screen.areas if a.type == 'IMAGE_EDITOR']
    viewers = [a for a in images if a.spaces.active.image and a.spaces.active.image.type == 'COMPOSITING']
    candidates = viewers or images
    if len(candidates) != 1:
        return None, None
    viewer = candidates[0]
    tolerance = max(8, 5 * bpy.context.preferences.system.ui_scale)
    neighbors = [a for a in screen.areas if a.ui_type == 'CompositorNodeTree'
                 and abs(a.x - viewer.x) <= 1 and abs(a.width - viewer.width) <= 1
                 and (0 <= viewer.y - (a.y + a.height) <= tolerance
                      or 0 <= a.y - (viewer.y + viewer.height) <= tolerance)]
    # Also recognize an old inverted layout so closing it repairs the next open.
    return (viewer, neighbors[0] if neighbors else None) if len(neighbors) <= 1 else (None, None)


def _configure_compositor_area(window, area):
    area.type = 'NODE_EDITOR'
    area.ui_type = 'CompositorNodeTree'
    _switch_region_defaults(area.spaces.active)
    area.spaces.active.pin = False
    area.spaces.active.show_region_ui = window.screen.get("photo_editor_node_sidebar", True)
    area.spaces.active.show_region_asset_shelf = False
    if area.spaces.active.show_region_ui:
        pending = window.workspace.get("photo_editor_pending_tabs", "").split()
        window.workspace["photo_editor_pending_tabs"] = " ".join(dict.fromkeys([*pending, "Node"]))
        finish_workspace_panels(window)
    area.tag_redraw()


def _defer_compositor_layout(window, screen, update):
    """Wait for Blender's screen refresh before reading split/join rectangles."""
    key = screen.as_pointer()
    attempts = 0

    def finish():
        nonlocal attempts
        attempts += 1
        try:
            if window.screen == screen and update():
                if _compositor_layout_timers.get(key) is finish:
                    _compositor_layout_timers.pop(key, None)
                return None
            if attempts < 100:
                return .025
        except (ReferenceError, RuntimeError):
            pass
        if _compositor_layout_timers.get(key) is finish:
            _compositor_layout_timers.pop(key, None)
        return None

    _compositor_layout_timers[key] = finish
    bpy.app.timers.register(finish, first_interval=.01)


def _toggle_compositor(window, viewer, nodes):
    screen = window.screen
    if nodes:
        # area_split measures between screen edges; RNA heights exclude the
        # separator. Counting it prevents the lower pane shrinking every toggle.
        lower, upper = sorted((viewer, nodes), key=lambda area: area.y)
        gap = upper.y - lower.y - lower.height
        extent = viewer.height + nodes.height + 2 * gap
        fraction = (nodes.height + gap + .5) / extent
        previous = screen.get("photo_editor_node_height", .31)
        if abs(previous - fraction) * extent < 2:
            fraction = previous
        sidebar = nodes.spaces.active.show_region_ui
        source = (viewer.x + viewer.width // 2, viewer.y + viewer.height // 2)
        target = (nodes.x + nodes.width // 2, nodes.y + nodes.height // 2)
        full_height = upper.y + upper.height - lower.y
        with bpy.context.temp_override(window=window, area=viewer):
            result = bpy.ops.screen.area_join(source_xy=source, target_xy=target)
        if 'FINISHED' in result:
            screen["photo_editor_node_height"] = fraction
            screen["photo_editor_node_sidebar"] = sidebar
            def finish_join():
                joined, remaining = viewer_column(screen)
                return bool(joined and not remaining and joined.height >= full_height - 2)
            _defer_compositor_layout(window, screen, finish_join)
        return result
    previous = {a.as_pointer() for a in window.screen.areas}
    original_y, original_top = viewer.y, viewer.y + viewer.height
    original_pointer = viewer.as_pointer()
    _split(window, viewer, 'HORIZONTAL', min(.95, max(.05, screen.get("photo_editor_node_height", .31))))
    added = next((a for a in window.screen.areas if a.as_pointer() not in previous), None)
    if not added:
        return {'CANCELLED'}
    split_pointers = {original_pointer, added.as_pointer()}

    def finish_split():
        pair = [a for a in screen.areas if a.as_pointer() in split_pointers]
        if len(pair) != 2:
            return True
        lower, upper = sorted(pair, key=lambda area: area.y)
        # area_split can return both areas with the old y/height values. In
        # particular, the original area changes sides when factor crosses .5.
        # Only choose the lower editor after both rectangles form the partition.
        if (lower.y == upper.y or lower.width <= 0 or upper.width <= 0
                or abs(lower.x - upper.x) > 1 or abs(lower.width - upper.width) > 1
                or abs(lower.y - original_y) > 2
                or abs(upper.y + upper.height - original_top) > 2
                or not 0 <= upper.y - lower.y - lower.height <= 8):
            return False
        _configure_compositor_area(window, lower)
        return True

    _defer_compositor_layout(window, screen, finish_split)
    return {'FINISHED'}


class BR_OT_toggle_compositor(bpy.types.Operator):
    bl_idname = "blender_raw.toggle_compositor"
    bl_label = "Toggle Compositor"
    bl_description = "Show or hide the node editor below the photo preview"
    effect_name: StringProperty(name="Scene Effect", options={'SKIP_SAVE'})

    def execute(self, context):
        if context.screen.as_pointer() in _compositor_layout_timers:
            return {'CANCELLED'}
        viewer, nodes = viewer_column(context.screen)
        if not viewer or not context.scene.get(compositor.STUDIO_TAG):
            return {'CANCELLED'}
        if self.effect_name:
            effect = context.scene.compositor_effects.get(self.effect_name)
            if not effect or not effect.node_group:
                return {'CANCELLED'}
            displayed = nodes.spaces.active.node_tree if nodes else None
            context.scene.compositor_effects.active = effect
            if nodes and displayed != effect.node_group:
                nodes.spaces.active.pin = False
                nodes.spaces.active.path.start(effect.node_group)
                nodes.tag_redraw()
                if nodes.y < viewer.y:
                    return {'FINISHED'}
                # Repair an inverted pane from an earlier split without losing
                # its height when selecting a different effect.
                full_height = nodes.y + nodes.height - viewer.y
                result = _toggle_compositor(context.window, viewer, nodes)
                if 'FINISHED' not in result:
                    return result
                window, screen = context.window, context.screen

                def reopen_below():
                    joined, remaining = viewer_column(screen)
                    if not joined or remaining or joined.height < full_height - 2:
                        return False
                    _compositor_layout_timers.pop(screen.as_pointer(), None)
                    _toggle_compositor(window, joined, None)
                    return True

                _defer_compositor_layout(window, screen, reopen_below)
                return {'FINISHED'}
        return _toggle_compositor(context.window, viewer, nodes)


def _stack_area(area):
    area.type = 'PROPERTIES'
    area.spaces.active.context = 'COMPOSITOR'
    area.spaces.active.pin_id = None
    area.tag_redraw()


def _default_properties_navigation(window):
    """Apply the Photo Workspace default once, preserving subsequent user choices."""
    if bpy.app.background:
        return
    screen = window.screen
    key = screen.as_pointer()
    if screen.get("photo_editor_navigation_default") or key in _navigation_timers:
        return
    attempts = 0

    def hide_navigation():
        nonlocal attempts
        attempts += 1
        try:
            if window.screen != screen:
                _navigation_timers.pop(key, None)
                return None
            panels = [area for area in screen.areas if area.type == 'PROPERTIES']
            regions = [(area, next((r for r in area.regions if r.type == 'NAVIGATION_BAR'), None),
                        next((r for r in area.regions if r.type == 'WINDOW'), None)) for area in panels]
            # SpaceProperties has no navigation visibility RNA property in 5.3.
            # Wait for its native regions to be laid out, then use the same
            # region-toggle operator as Navigation Bar > Hide.
            if not regions or any(not main or main.width <= 1 or main.height <= 1
                                  for _, _, main in regions):
                if attempts < 40:
                    return .05
            else:
                for area, navigation, main in regions:
                    if navigation and navigation.width > 1:
                        with bpy.context.temp_override(window=window, area=area, region=main):
                            if 'FINISHED' not in bpy.ops.screen.region_toggle(region_type='NAVIGATION_BAR'):
                                if attempts < 40:
                                    return .05
                                _navigation_timers.pop(key, None)
                                return None
                screen["photo_editor_navigation_default"] = True
        except (ReferenceError, RuntimeError):
            pass
        _navigation_timers.pop(key, None)
        return None

    _navigation_timers[key] = hide_navigation
    bpy.app.timers.register(hide_navigation, first_interval=.1)


def _configure_editors(window, areas):
    left, top, right = sorted(areas, key=lambda area: area.x)
    _gallery_area(left)
    left.spaces.active.show_region_toolbar = False
    left.spaces.active.show_region_ui = False
    top.type = 'IMAGE_EDITOR'
    top.spaces.active.mode = 'VIEW'
    top.spaces.active.show_region_ui = True
    viewer = compositor.viewer_image()
    if viewer:
        top.spaces.active.image = viewer
    _stack_area(right)
    window.workspace["blender_raw_workspace"] = True
    window.workspace["photo_editor_default_layout"] = True
    window.screen["photo_editor_native_layout"] = True
    window.screen["photo_editor_navigation_default"] = False
    window.workspace["photo_editor_pending_tabs"] = "Scopes"
    _activate_photo_workspace(window)
    finish_workspace_panels(window)


def _activate_photo_workspace(window):
    workspace = window.workspace
    if not workspace.get("blender_raw_workspace") or not workspace.is_editable:
        return
    _default_properties_navigation(window)
    workspace.use_pin_scene = True
    if not any(s.blender_raw.owner for s in bpy.data.scenes if not s.library):
        window.scene.blender_raw.owner = True
    if not window.scene.get(compositor.STUDIO_TAG):
        with bpy.context.temp_override(window=window):
            lib = catalog.library(bpy.context)
            if 0 <= lib.active_index < len(lib.photos):
                compositor.open_photo(bpy.context, lib.photos[lib.active_index])
    for area in window.screen.areas:
        if area.type == 'IMAGE_EDITOR':
            image = area.spaces.active.image
            if image and image.type == 'COMPOSITING':
                image.use_view_as_render = True


def _sync_active_workspaces():
    for window in bpy.context.window_manager.windows:
        _activate_photo_workspace(window)


@persistent
def _restore_workspaces(*_):
    for workspace in bpy.data.workspaces:
        if workspace.get("blender_raw_workspace") and not workspace.library and workspace.name in {"Blender Raw", "Blender Photo Editor", "ShawnHu Photo Editor"}:
            workspace.name = WORKSPACE_NAME
    if not bpy.app.background:
        # Blender clears RNA subscriptions when loading a file.
        bpy.msgbus.clear_by_owner(_workspace_subscription)
        bpy.msgbus.subscribe_rna(key=(bpy.types.Window, "workspace"), owner=_workspace_subscription,
                                 args=(), notify=_sync_active_workspaces)
        _sync_active_workspaces()
        for window in bpy.context.window_manager.windows:
            finish_workspace_panels(window)


def _confirm_workspace(self, context, event):
    return context.window_manager.invoke_confirm(
        self, event, title="Open Photo Workspace?", icon='WARNING',
        message="Turn off automatic packing and unpack this project's resources to disk?",
        confirm_text="Unpack and Open")


class BR_OT_open_workspace(bpy.types.Operator):
    bl_idname = "blender_raw.open_workspace"
    bl_label = "Open Photo Workspace"
    bl_description = "Open Photo Gallery, the photo preview and Photo Editor together"
    new_workspace: BoolProperty(options={'HIDDEN', 'SKIP_SAVE'})

    invoke = _confirm_workspace

    def execute(self, context):
        from . import workspace_resources
        try:
            workspace_resources.unpack_project(context)
        except (OSError, RuntimeError, ValueError) as exc:
            self.report({'ERROR'}, f"Could not finish unpacking resources: {exc}")
            return {'CANCELLED'}
        window = context.window
        previous = window.workspace
        existing = None
        if not self.new_workspace:
            workspaces = [w for w in bpy.data.workspaces if w.get("blender_raw_workspace")]
            if previous in workspaces:
                existing = previous
            elif workspaces:
                existing = next((w for w in workspaces if w.get("photo_editor_default_layout")), workspaces[0])
        if existing:
            window.workspace = existing
        else:
            try:
                _append_workspace_template(window)
            except (OSError, RuntimeError) as exc:
                self.report({'ERROR'}, f"Could not create the photo workspace: {exc}")
                return {'CANCELLED'}
        attempts = 0
        phase = 'WAIT'
        created = None

        def configure():
            nonlocal attempts, phase, created
            try:
                if phase == 'WAIT':
                    attempts += 1
                    if (existing and window.workspace != existing) or (not existing and window.workspace == previous):
                        if attempts < 40:
                            return .05
                        _timers.discard(configure)
                        return None
                    created = window.workspace
                    if existing:
                        _activate_photo_workspace(window)
                        finish_workspace_panels(window)
                        _timers.discard(configure)
                        return None
                    else:
                        created.name = WORKSPACE_NAME
                    phase = 'SPLIT_GALLERY'
                if window.workspace != created:
                    _timers.discard(configure)
                    return None
                areas = sorted(window.screen.areas, key=lambda area: area.x)
                scale = max(.75, bpy.context.preferences.system.ui_scale)
                if phase == 'SPLIT_GALLERY':
                    if len(areas) != 1 or areas[0].type != 'IMAGE_EDITOR':
                        raise RuntimeError("Photo Workspace template must contain one Image Editor")
                    main = areas[0]
                    size = catalog.library(bpy.context).thumbnail_scale
                    fraction = _gallery_fraction(main.width, scale, _thumbnail_limit(bpy.context, size))
                    # Keep the template's original Image Editor on the right.
                    # Native splits reset Scopes heights in the copied editor.
                    _split(window, main, 'VERTICAL', min(.49, fraction))
                    phase = 'SPLIT_EFFECTS'
                    return .12
                if phase == 'SPLIT_EFFECTS':
                    remaining = max(areas, key=lambda area: area.x)
                    panel_fraction = min(.38, 330 * scale / remaining.width)
                    _split(window, remaining, 'VERTICAL', 1 - panel_fraction)
                    phase = 'ASSIGN'
                    return .12
                _configure_editors(window, areas)
            except (ReferenceError, RuntimeError, ValueError) as exc:
                print(f"RAW Photo Editor workspace: {exc}")
            _timers.discard(configure)
            return None

        _timers.add(configure)
        bpy.app.timers.register(configure,first_interval=.1)
        return {'FINISHED'}


class BR_OT_new_workspace(bpy.types.Operator):
    bl_idname = "blender_raw.new_workspace"
    bl_label = "New Photo Editing Workspace"
    bl_description = "Create the default photo editing layout in a new workspace tab"

    invoke = _confirm_workspace

    def execute(self, context):
        return bpy.ops.blender_raw.open_workspace(new_workspace=True)


def _menu_workspace(self,context):
    buttons = self.layout.column()
    buttons.operator_context = 'INVOKE_DEFAULT'
    buttons.operator("blender_raw.open_workspace", icon='IMAGE_DATA')
    if any(workspace.get("blender_raw_workspace") for workspace in bpy.data.workspaces):
        buttons.operator("blender_raw.new_workspace", icon='ADD')


@persistent
def _clear_cache(*_):
    global _hover_window
    _hover_window = None
    _textures.clear()
    _offsets.clear()
    _scroll_grids.clear()
    _scroll_proximity.clear()
    _pressed_bars.clear()
    _view_geometry.clear()
    _anchors.clear()
    _context_photos.clear()
    _boxes.clear()
    _ordinary_regions.clear()
    _region_modes.clear()
    _gallery_regions.clear()
    _new_sidebars.clear()
    _status_regions.clear()
    _panel_timers.clear()
    for callback in tuple(_compositor_layout_timers.values()):
        if bpy.app.timers.is_registered(callback):
            bpy.app.timers.unregister(callback)
    _compositor_layout_timers.clear()
    for callback in tuple(_navigation_timers.values()):
        if bpy.app.timers.is_registered(callback):
            bpy.app.timers.unregister(callback)
    _navigation_timers.clear()


_classes = (BR_GalleryPreferences,BlenderRawGalleryTree,BR_OT_gallery_click,BR_OT_gallery_navigate,BR_OT_gallery_scrollbar,BR_OT_rate_photos,
            BR_OT_flag_photos,BR_OT_copy_adjustments,BR_OT_paste_adjustments,BR_MT_gallery_photo,BR_OT_gallery_context,
            BR_OT_gallery_filter,BR_OT_gallery_details,BR_OT_edit_gallery_tags,
            BR_PT_gallery_display,BR_PT_gallery_sidebar,
            BR_OT_open_gallery,
            BR_OT_open_workspace, BR_OT_new_workspace, BR_OT_toggle_compositor)


def register():
    global _handle
    for cls in _classes:
        bpy.utils.register_class(cls)
    _install_header()
    bpy.types.TOPBAR_MT_window.append(_menu_workspace)
    bpy.app.handlers.load_pre.append(adjustments.clear)
    bpy.app.handlers.load_pre.append(_clear_cache)
    bpy.app.handlers.exit_pre.append(_clear_cache)
    bpy.app.handlers.load_post.append(_restore_workspaces)
    if not bpy.app.background:
        bpy.types.STATUSBAR_HT_header.append(_statusbar_export)
        edited_preview.register()
        _handle = bpy.types.SpaceNodeEditor.draw_handler_add(_draw_canvas,(), 'WINDOW','POST_PIXEL')
        bpy.app.handlers.load_post.append(_use_saved_region_visibility)
        bpy.app.timers.register(_use_saved_region_visibility)
        bpy.app.timers.register(_restore_workspaces)
    config = bpy.context.window_manager.keyconfigs.addon
    if config:
        # Node Generic runs before tools belonging to an editable node tree.
        km = config.keymaps.new(name='Node Generic',space_type='NODE_EDITOR',region_type='WINDOW')
        item = km.keymap_items.new(BR_OT_gallery_click.bl_idname,'LEFTMOUSE','PRESS',any=True,head=True)
        _keymaps.append((km,item))
        item = km.keymap_items.new(BR_OT_gallery_context.bl_idname,'RIGHTMOUSE','PRESS',head=True)
        _keymaps.append((km,item))
        for key in ('X', 'DEL'):
            item = km.keymap_items.new('blender_raw.remove_photo', key, 'PRESS', head=True)
            _keymaps.append((km, item))
        item = km.keymap_items.new(BR_OT_gallery_click.bl_idname,'B','PRESS',head=True)
        item.properties.wait_for_input = True
        _keymaps.append((km,item))
        for key, flagged in (('P', True), ('U', False)):
            item = km.keymap_items.new(BR_OT_flag_photos.bl_idname,key,'PRESS',head=True)
            item.properties.flagged = flagged
            _keymaps.append((km,item))
        for key,action,ctrl,alt in (
            ('LEFT_ARROW','LEFT',False,False),('RIGHT_ARROW','RIGHT',False,False),
            ('UP_ARROW','UP',False,False),('DOWN_ARROW','DOWN',False,False),
            ('A','ALL',True,False),('A','NONE',False,True)):
            item = km.keymap_items.new(BR_OT_gallery_navigate.bl_idname,key,'PRESS',ctrl=ctrl,alt=alt,head=True)
            item.properties.action = action
            _keymaps.append((km,item))
        # View2D handles the wheel before Node Generic and normally zooms the canvas.
        # The operator's poll confines these bindings to a Gallery WINDOW region.
        view_keys = config.keymaps.new(name='View2D', space_type='EMPTY', region_type='WINDOW')
        for key, value, action in (('WHEELUPMOUSE', 'PRESS', 'SCROLL_UP'), ('WHEELDOWNMOUSE', 'PRESS', 'SCROLL_DOWN'),
                                   ('TRACKPADPAN', 'ANY', 'PAN')):
            item = view_keys.keymap_items.new(BR_OT_gallery_navigate.bl_idname, key, value, head=True)
            item.properties.action = action
            _keymaps.append((view_keys, item))
        for key, value in (('LEFTMOUSE', 'PRESS'), ('MIDDLEMOUSE', 'PRESS')):
            item = view_keys.keymap_items.new(BR_OT_gallery_scrollbar.bl_idname, key, value, any=True, head=True)
            _keymaps.append((view_keys, item))
        window_keys = config.keymaps.new(name='Window', space_type='EMPTY', region_type='WINDOW')
        item = window_keys.keymap_items.new(BR_OT_gallery_scrollbar.bl_idname, 'MOUSEMOVE', 'ANY', any=True, head=True)
        _keymaps.append((window_keys, item))


def unregister():
    global _handle, _solid, _image_shader
    bpy.msgbus.clear_by_owner(_workspace_subscription)
    if _use_saved_region_visibility in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_use_saved_region_visibility)
    bpy.app.handlers.load_post.remove(_restore_workspaces)
    for callback in (_use_saved_region_visibility, _on_editor_change, _select_initial_sidebar,
                     _restore_workspaces, *_panel_timers.values()):
        if bpy.app.timers.is_registered(callback):
            bpy.app.timers.unregister(callback)
    for screen in bpy.data.screens:
        for area in screen.areas:
            for space in area.spaces:
                if space.type == 'NODE_EDITOR' and (previous := _ordinary_regions.get(space.as_pointer())) is not None:
                    space.show_region_toolbar, space.show_region_ui = previous
    _restore_header()
    for callback in tuple(_timers):
        if bpy.app.timers.is_registered(callback):
            bpy.app.timers.unregister(callback)
    _timers.clear()
    if _handle is not None:
        bpy.types.STATUSBAR_HT_header.remove(_statusbar_export)
        edited_preview.unregister()
        bpy.types.SpaceNodeEditor.draw_handler_remove(_handle,'WINDOW')
        _handle = None
    _clear_cache()
    _solid = _image_shader = None
    for km,item in reversed(_keymaps):
        km.keymap_items.remove(item)
    _keymaps.clear()
    bpy.app.handlers.load_pre.remove(adjustments.clear)
    adjustments.clear()
    bpy.app.handlers.load_pre.remove(_clear_cache)
    bpy.app.handlers.exit_pre.remove(_clear_cache)
    bpy.types.TOPBAR_MT_window.remove(_menu_workspace)
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
