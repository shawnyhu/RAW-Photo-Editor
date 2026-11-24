# SPDX-License-Identifier: GPL-3.0-or-later
"""Display and select camera originals in the native import dialog's thumbnail view."""
from array import array
from dataclasses import dataclass
from fnmatch import fnmatch
from math import ceil
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import time

import bpy
import bpy.utils.previews
import gpu
from gpu_extras.batch import batch_for_shader
from gpu_extras.presets import draw_texture_2d

from . import catalog, filters, jobs


_sessions = {}
_handler = None


@dataclass
class Entry:
    name: str
    directory: bool
    size: int
    modified: int


class _GridBox:
    """Keep the origin fixed in content coordinates as the file grid scrolls."""
    def __init__(self, x, y, height, offset, index, event):
        self.press = self.pointer = (x, y)
        self.origin_depth = height - y + offset
        self.index = index
        self.modified = (event.shift, event.ctrl or event.oskey)
        self.double = event.value == 'DOUBLE_CLICK'
        self.baseline = None
        self.rect = self.rows = self.cols = None

    def update(self, session, region, x, y):
        self.pointer = x, y
        end_x = max(0, min(region.width - 1, x))
        end_y = max(0, min(region.height - 1, y))
        left, right = sorted((self.press[0], end_x))
        origin_y = region.height + session.offset - self.origin_depth
        low, high = sorted((max(0, min(region.height - 1, origin_y)), end_y))
        self.rect = left, low, right, high
        near, far = sorted((self.origin_depth, region.height + session.offset - end_y))
        stride = session.tile_width + session.gap
        self.cols = range(max(0, (left - session.gap - session.tile_width) // stride + 1),
                          min(session.columns, (right - session.gap) // stride + 1))
        self.rows = range(max(0, near // session.row_h),
                          min(ceil(len(session.entries) / session.columns), (far - session.gap) // session.row_h + 1))

    def includes(self, session, index):
        entry = session.entries[index]
        row, col = divmod(index, session.columns)
        hit = not entry.directory and row in self.rows and col in self.cols
        add, subtract = self.modified
        if subtract:
            return entry.name in self.baseline and not hit
        return hit or (add and entry.name in self.baseline)

    def result(self, session):
        hits = set()
        for row in self.rows:
            for col in self.cols:
                index = row * session.columns + col
                if index < len(session.entries) and not session.entries[index].directory:
                    hits.add(session.entries[index].name)
        add, subtract = self.modified
        if subtract:
            return self.baseline - hits
        return self.baseline | hits if add else hits


def _entries(directory, search, hidden, sort, reverse, depth):
    folders, files = [], []
    todo = [(directory, 0)]
    while todo:
        parent, level = todo.pop()
        try:
            with os.scandir(parent) as children:
                for child in children:
                    if not hidden and child.name.startswith('.'):
                        continue
                    try:
                        info = child.stat(follow_symlinks=False)
                        if not hidden and getattr(info, 'st_file_attributes', 0) & 2:
                            continue
                        folder = child.is_dir()
                        if folder and level < depth and not child.is_symlink():
                            todo.append((Path(child.path), level + 1))
                        if not folder and Path(child.name).suffix.lower()[1:] not in filters.RAW_SUFFIXES:
                            continue
                        if search and not fnmatch(child.name.casefold(), '*' + search.casefold() + '*'):
                            continue
                        entry = Entry(os.path.relpath(child.path, directory), folder, info.st_size, info.st_mtime_ns)
                        (folders if folder else files).append(entry)
                    except OSError:
                        continue
        except OSError:
            if parent == directory:
                raise
    def order(entry):
        name = entry.name.casefold()
        if sort == 'FILE_SORT_TIME':
            return -entry.modified, name
        if sort == 'FILE_SORT_SIZE':
            return -entry.size, name
        if sort == 'FILE_SORT_EXTENSION':
            return Path(name).suffix, name
        return name
    return sorted(folders, key=order, reverse=reverse) + sorted(files, key=order, reverse=reverse)


class _Session:
    def __init__(self):
        try:
            self.cache = catalog.cache_dir() / 'import-previews-v1'
            self.cache.mkdir(exist_ok=True)
            self.folder = TemporaryDirectory(prefix='.session-', dir=self.cache)
        except OSError:
            self.folder = TemporaryDirectory(prefix='BlenderPhotoEditor-import-')
            self.cache = Path(self.folder.name) / 'cache'
            self.cache.mkdir()
        self.icons = bpy.utils.previews.new()
        self.textures = {}
        self.job = self.scan = self.directory = self.anchor = None
        self.entries = []
        self.failed = set()
        self.repair_attempted = set()
        self.pending_icons = False
        self.offset = 0
        self.opened = False
        self.created = time.monotonic()
        self.next_stat = self.folder_time = 0
        self.error = ''
        self.box = None
        self.box_armed = False

    @property
    def box_rect(self):
        return self.box.rect if self.box else None

    def box_includes(self, index):
        return self.box.includes(self, index)

    def cancel_box(self):
        self.box = None
        self.box_armed = False

    def scroll_box(self, context):
        if self.box_rect is None or not self.maximum:
            return False
        region = context.region
        x, y = self.box.pointer
        edge = max(8, min(self.row_h // 3, region.height // 5))
        speed = min(1, max(0, (edge-y)/edge)) - min(1, max(0, (y-region.height+edge)/edge))
        offset = max(0, min(self.maximum, self.offset + round(speed * self.row_h * .65)))
        if offset == self.offset:
            return False
        self.offset = offset
        self.box.update(self, region, x, y)
        return True

    def refresh(self, params):
        directory = Path(os.fsdecode(params.directory))
        scan = (directory, params.filter_search, params.show_hidden, params.sort_method, params.use_sort_invert,
                {'ALL_1': 1, 'ALL_2': 2, 'ALL_3': 3}.get(params.recursion_level, 0))
        now = time.monotonic()
        if scan == self.scan and now < self.next_stat:
            return False
        self.next_stat = now + 1
        try:
            modified = directory.stat().st_mtime_ns
        except OSError:
            modified = None
        if scan == self.scan and self.folder_time == modified:
            return False
        if directory != self.directory:
            self.stop_worker()
        self.anchor = None
        self.cancel_box()
        self.scan, self.directory, self.folder_time, self.offset = scan, directory, modified, 0
        try:
            self.entries = _entries(*scan)
            self.error = ''
        except OSError:
            self.entries = []
            self.error = 'Cannot read this folder'
        return True

    def key(self, entry):
        path = os.path.normcase(os.path.abspath(self.directory / entry.name))
        return catalog._digest(f'{path}:{entry.size}:{entry.modified}')

    def tiles(self, context):
        region = context.region
        scale = context.preferences.system.ui_scale
        width, height = region.width, region.height
        gap = max(4, round(8 * scale))
        side = max(60, round(context.space_data.params.display_size * scale))
        available = width - 2 * gap
        self.columns = max(1, available // (side + gap))
        tile_w = max(1, (available - self.columns * gap) // self.columns)
        self.tile_width, self.gap = tile_w, gap
        self.row_h = side + round(24 * scale) + gap
        self.maximum = max(0, ceil(len(self.entries) / self.columns) * self.row_h + gap - height)
        self.offset = max(0, min(self.maximum, self.offset))
        start = (self.offset // self.row_h) * self.columns
        tiles = []
        for i in range(start, len(self.entries)):
            row, col = divmod(i, self.columns)
            top = height - gap - row * self.row_h + self.offset
            if top <= 0:
                break
            tiles.append((i, gap + col * (tile_w + gap), top - self.row_h + gap, tile_w, self.row_h - gap))
        return tiles

    def stop_worker(self):
        if self.job:
            process, scratch, _ = self.job
            if process.poll() is None:
                process.kill()
            process.wait()
            scratch.cleanup()
            self.job = None

    def pump(self, visible):
        changed = False
        self.pending_icons = False
        if self.job and self.job[0].poll() is not None:
            process, scratch, submitted = self.job
            if process.returncode:
                # A native decoder can exit midway through a batch. Keep files
                # it published and give the following originals their own turn.
                for key in submitted:
                    if not (self.cache / (key + '.jpg')).is_file() and not (Path(self.folder.name) / (key + '.failed')).is_file():
                        self.failed.add(key)
                        break
            scratch.cleanup()
            self.job = None
        entries = {self.key(entry): entry for entry in visible if not entry.directory}
        capacity = max(128, len(entries))
        offscreen = iter(key for key in tuple(self.icons) if key not in entries)

        def evict_offscreen():
            key = next(offscreen)
            self.textures.pop(key, None)
            del self.icons[key]

        while len(self.icons) > capacity:
            evict_offscreen()
        wanted = {}
        loaded = 0
        deadline = time.monotonic() + .012
        for key, entry in entries.items():
            if key in self.failed or key in self.icons:
                continue
            path = self.cache / (key + '.jpg')
            staging = Path(self.folder.name) / (key + '.jpg')
            if path.is_file():
                if loaded >= 4 and (loaded >= 12 or time.monotonic() >= deadline):
                    self.pending_icons = True
                    continue
                try:
                    if len(self.icons) >= capacity:
                        evict_offscreen()
                    # Also check the JPEG on disk: Blender can have its own
                    # valid thumbnail of a cache file that was later truncated.
                    with path.open('rb') as file:
                        if file.read(2) != b'\xff\xd8':
                            raise ValueError("Invalid thumbnail")
                        file.seek(-2, os.SEEK_END)
                        if file.read(2) != b'\xff\xd9':
                            raise ValueError("Incomplete thumbnail")
                    preview = self.icons.load(key, str(path), 'IMAGE', force_reload=True)
                    if not all(preview.image_size):
                        del self.icons[key]
                        raise ValueError("Unreadable cached thumbnail")
                    loaded += 1
                except (OSError, RuntimeError, ValueError):
                    # Regenerate an invalid disk-cache entry from the original.
                    if key in self.repair_attempted:
                        self.failed.add(key)
                    self.repair_attempted.add(key)
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        self.failed.add(key)
                changed = True
            elif staging.with_suffix('.failed').exists():
                self.failed.add(key)
                changed = True
            else:
                wanted[key] = dict(mode='import_thumb', source=str(self.directory / entry.name),
                                   output=str(staging), cache=str(path), size=entry.size, modified=entry.modified)
        if wanted and self.job is None:
            # Keep one decoder alive for a small batch. Reprioritize the visible page after scrolling.
            batch = dict(list(wanted.items())[:8])
            try:
                process, scratch = jobs._start(dict(mode='import_thumbnails', items=list(batch.values()),
                                                    cache_dir=str(self.cache)))
                self.job = process, scratch, tuple(batch)
            except (OSError, RuntimeError):
                self.failed.update(batch)
                changed = True
        return changed

    def texture(self, key):
        if key not in self.textures:
            preview = self.icons.get(key)
            if not preview:
                return None
            w, h = preview.image_size
            if not w or not h:
                return None
            pixels = array('f', [0]) * (w * h * 4)
            preview.image_pixels_float.foreach_get(pixels)
            buffer = gpu.types.Buffer('FLOAT', len(pixels), pixels)
            self.textures[key] = gpu.types.GPUTexture((w, h), format='RGBA16F', data=buffer)
        return self.textures[key]

    def close(self):
        self.stop_worker()
        self.textures.clear()
        bpy.utils.previews.remove(self.icons)
        self.folder.cleanup()


def _current(context):
    space = context.space_data
    if not space or space.type != 'FILE_BROWSER' or not space.active_operator or not space.params:
        return None
    if space.params.display_type != 'THUMBNAIL':
        return None
    return _sessions.get(space.active_operator.as_pointer())


def _outline(shader, x, y, w, h, thickness, color):
    t = min(thickness, w / 2, h / 2)
    if t <= 0:
        return
    corners = ((x, y), (x+t, y+t), (x+w, y), (x+w-t, y+t),
               (x+w, y+h), (x+w-t, y+h-t), (x, y+h), (x+t, y+h-t))
    shader.bind()
    shader.uniform_float('color', color)
    batch_for_shader(shader, 'TRI_STRIP', {'pos': corners + corners[:2]}).draw(shader)


def _selection_mark(shader, x, y, scale, accent):
    from .gallery import _rectangle
    side = 18 * scale
    _rectangle(shader, (x, y, side, side), (.08, .12, .18, 1))
    _outline(shader, x, y, side, side, max(1, scale), accent)
    tick = ((4.5, 10), (3, 8.5), (7.5, 7), (7.5, 4), (13.5, 13), (15, 11.5))
    vertices = [(x + dx*scale, y + dy*scale) for dx, dy in tick]
    shader.bind()
    shader.uniform_float('color', (1, 1, 1, 1))
    batch_for_shader(shader, 'TRI_STRIP', {'pos': vertices}).draw(shader)


def _draw():
    from .gallery import _label, _rectangle
    context = bpy.context
    session = _current(context)
    if not session:
        return
    region, theme = context.region, context.preferences.themes[0]
    scale = context.preferences.system.ui_scale
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    back = (*theme.file_browser.space.back[:3], 1)
    ink = (*theme.file_browser.space.text[:3], 1)
    item = theme.user_interface.wcol_list_item
    accent = (*item.inner_sel[:3], 1)
    edge = tuple(c * .76 + .24 for c in accent[:3]) + (1,)
    selected_ink = (*item.text_sel[:3], 1)
    box = session.box_rect
    selected = _native_selection(context) if box is None else None
    blend = gpu.state.blend_get()
    try:
        gpu.state.blend_set('ALPHA')
        _rectangle(shader, (0, 0, region.width, region.height), back)
        if session.directory != Path(os.fsdecode(context.space_data.params.directory)):
            return
        for i, x, y, w, h in session.tiles(context):
            entry = session.entries[i]
            chosen = session.box_includes(i) if box is not None else entry.name in selected
            if chosen:
                _rectangle(shader, (x, y, w, h), accent)
            caption = round(23 * scale)
            iw, ih = max(1, w - 8 * scale), max(1, h - caption - 4 * scale)
            if entry.directory:
                fw, fh = min(iw, ih) * .65, min(iw, ih) * .45
                fx, fy = x + (w-fw)/2, y + caption + (ih-fh)/2
                color = (*theme.user_interface.wcol_tool.text[:3], .72)
                _rectangle(shader, (fx, fy, fw, fh), color)
                _rectangle(shader, (fx, fy + fh, fw*.42, fh*.14), color)
            else:
                key = session.key(entry)
                texture = session.texture(key)
                if texture is not None:
                    ratio = min(iw / texture.width, ih / texture.height)
                    tw, th = texture.width * ratio, texture.height * ratio
                    draw_texture_2d(texture, (x + (w-tw)/2, y + caption + (ih-th)/2), tw, th)
                else:
                    text = 'Unavailable' if key in session.failed else 'Loading…'
                    _label(text, x + 6*scale, y + caption + ih/2, 11*scale, ink, width=iw)
            _label(entry.name, x + 5*scale, y + 6*scale, 12*scale, selected_ink if chosen else ink, width=w-10*scale)
            gpu.state.blend_set('ALPHA')
            if chosen:
                _outline(shader, x, y, w, h, max(2, round(2*scale)), edge)
                _selection_mark(shader, x + 3*scale, y + h - 21*scale, scale, edge)
        if not session.entries:
            _label(session.error or 'No RAW files or folders', 14*scale, region.height-26*scale, 13*scale, ink)
        if session.maximum:
            thumb = max(20*scale, region.height**2/(region.height+session.maximum))
            top = region.height - session.offset / session.maximum * (region.height-thumb)
            _rectangle(shader, (region.width-7*scale, top-thumb, 5*scale, thumb), (*ink[:3], .55))
        if box is not None:
            left, bottom, right, top = box
            gpu.state.blend_set('ALPHA')
            _rectangle(shader, (left, bottom, right-left, top-bottom), (*accent[:3], .16))
            _outline(shader, left, bottom, right-left, top-bottom, max(1, round(scale)), edge)
    finally:
        gpu.state.blend_set(blend)


def _native_selection(context):
    return {item.relative_path for item in (context.selected_files or ())}


def _apply_selection(context, previous, names, active=None):
    space = context.space_data
    # RNA can add an individual entry or clear the whole selection. Apply changes
    # once on release, keeping the existing native selection for additive gestures.
    if previous - names:
        space.deselect_all()
        previous = set()
    additions = names - previous
    for name in sorted(additions - {active}):
        space.activate_file_by_relative_path(relative_path=name)
    if active in names:
        space.activate_file_by_relative_path(relative_path=active)
    if len(names) == 1:
        name = next(iter(names))
        space.params.filename = '' if os.path.isdir(os.path.join(os.fsdecode(space.params.directory), name)) else name
    else:
        space.params.filename = ''
    context.area.tag_redraw()


class BR_OT_import_grid_events(bpy.types.Operator):
    bl_idname = 'blender_raw.import_grid_events'
    bl_label = 'Browse RAW thumbnails'
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        self.key = context.space_data.active_operator.as_pointer()
        self.drag_scroll = False
        self.last_click = None
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        session = _sessions.get(self.key)
        if not session:
            return {'CANCELLED'}
        if _current(context) != session:
            session.cancel_box()
            self.drag_scroll = False
            return {'PASS_THROUGH'}
        region = context.region
        x, y = event.mouse_x - region.x, event.mouse_y - region.y
        if (session.box or session.box_armed) and event.type in {'ESC', 'RIGHTMOUSE', 'WINDOW_DEACTIVATE'}:
            session.cancel_box()
            self.last_click = None
            context.area.tag_redraw()
            return {'PASS_THROUGH'} if event.type == 'WINDOW_DEACTIVATE' else {'RUNNING_MODAL'}
        if session.box:
            box = session.box
            if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE', 'LEFTMOUSE'}:
                if event.value == 'DOUBLE_CLICK':
                    box.double = True
                self._move_box(context, session, x, y, event)
                if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
                    session.cancel_box()
                    if box.baseline is not None:
                        names = box.result(session)
                        _apply_selection(context, box.baseline, names)
                        session.anchor = box.index
                        self.last_click = None
                    elif box.index is not None:
                        self._click(context, session, box)
                    else:
                        _apply_selection(context, _native_selection(context), set())
                        session.anchor = self.last_click = None
                return {'RUNNING_MODAL'}
            return {'RUNNING_MODAL'}
        if self.drag_scroll and event.type in {'MOUSEMOVE', 'LEFTMOUSE'}:
            if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
                self.drag_scroll = False
            else:
                self._scroll(session, region, y, context)
            return {'RUNNING_MODAL'}
        if not (0 <= x < region.width and 0 <= y < region.height):
            return {'PASS_THROUGH'}
        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            return {'RUNNING_MODAL'}
        if event.type == 'B':
            if event.value == 'PRESS':
                session.box_armed = True
                self.last_click = None
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value in {'PRESS', 'DOUBLE_CLICK'}:
            session.refresh(context.space_data.params)
        tiles = session.tiles(context)
        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN'}:
            delta = (event.mouse_y-event.mouse_prev_y if event.type == 'TRACKPADPAN'
                     else session.row_h * (-1 if event.type == 'WHEELUPMOUSE' else 1))
            session.offset = max(0, min(session.maximum, session.offset + delta))
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE':
            if event.value in {'PRESS', 'DOUBLE_CLICK'}:
                if session.maximum and x >= region.width - 10*context.preferences.system.ui_scale:
                    self.drag_scroll = True
                    self._scroll(session, region, y, context)
                else:
                    index = next((i for i, tx, ty, w, h in tiles if tx <= x < tx+w and ty <= y < ty+h), None)
                    session.box = _GridBox(x, y, region.height, session.offset, index, event)
                    if session.box_armed:
                        self._move_box(context, session, x, y, event)
            return {'RUNNING_MODAL'}
        if event.type in {'RET', 'NUMPAD_ENTER', 'ESC', 'BACK_SPACE', 'BUTTON4MOUSE', 'BUTTON5MOUSE'}:
            return {'PASS_THROUGH'}
        if event.type == 'A' and event.ctrl and event.value == 'PRESS':
            bpy.ops.file.select_all(action='SELECT')
            return {'RUNNING_MODAL'}
        if event.type in {'UP_ARROW','DOWN_ARROW','LEFT_ARROW','RIGHT_ARROW'} and event.value == 'PRESS' and session.entries:
            active = context.active_file
            current = next((i for i, entry in enumerate(session.entries) if active and entry.name == active.relative_path), -1)
            delta = {'UP_ARROW': -session.columns, 'DOWN_ARROW': session.columns, 'LEFT_ARROW': -1, 'RIGHT_ARROW': 1}[event.type]
            index = max(0, min(len(session.entries)-1, current+delta))
            self._select(context, session, index, event.shift, event.ctrl or event.oskey)
            top = index // session.columns * session.row_h
            session.offset = max(top+session.row_h-region.height, min(top, session.offset))
            return {'RUNNING_MODAL'}
        if event.type in {'HOME','END','PAGE_UP','PAGE_DOWN'} and event.value == 'PRESS':
            if event.type in {'HOME','END'}:
                session.offset = 0 if event.type == 'HOME' else session.maximum
            else:
                session.offset = max(0, min(session.maximum, session.offset + region.height * (1 if event.type == 'PAGE_DOWN' else -1)))
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        # Native hit-testing uses different tile positions. Keep its mouse gestures out of this grid.
        if event.type in {'RIGHTMOUSE','MIDDLEMOUSE','ACTIONMOUSE','SELECTMOUSE','B','X','DEL','F2','MOUSEMOVE','INBETWEEN_MOUSEMOVE'}:
            return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}

    @staticmethod
    def _scroll(session, region, y, context):
        thumb = max(20*context.preferences.system.ui_scale, region.height**2/(region.height+session.maximum))
        session.offset = round(max(0, min(1, (region.height-y-thumb/2) / max(1, region.height-thumb))) * session.maximum)
        context.area.tag_redraw()

    @staticmethod
    def _move_box(context, session, x, y, event):
        box = session.box
        if box.baseline is None:
            inputs = context.preferences.inputs
            threshold = inputs.drag_threshold_tablet if event.is_tablet else inputs.drag_threshold_mouse
            threshold *= context.preferences.system.ui_scale
            if not session.box_armed and max(abs(x-box.press[0]), abs(y-box.press[1])) <= threshold:
                return
            box.baseline = _native_selection(context)
        box.update(session, context.region, x, y)
        context.area.tag_redraw()

    def _click(self, context, session, box):
        index = box.index
        entry = session.entries[index]
        x, y = box.press
        now, previous = time.monotonic(), self.last_click
        inputs = context.preferences.inputs
        same = previous and previous[:2] == (session.directory, entry.name)
        double = box.double or (same and now-previous[2] <= inputs.mouse_double_click_time / 1000
                                and max(abs(x-previous[3]), abs(y-previous[4])) <= inputs.drag_threshold_mouse)
        plain = not any(box.modified)
        self.last_click = (session.directory, entry.name, now, x, y) if plain and not double else None
        self._select(context, session, index, *box.modified)
        if plain and double and context.active_file and context.active_file.relative_path == entry.name:
            bpy.ops.file.execute()

    @staticmethod
    def _select(context, session, index, shift, toggle):
        entry = session.entries[index]
        previous = _native_selection(context)
        if shift and session.anchor is not None:
            a, b = sorted((session.anchor, index))
            names = {item.name for item in session.entries[a:b+1] if not item.directory}
        elif toggle:
            names = previous ^ {entry.name}
            session.anchor = index
        else:
            names = {entry.name}
            session.anchor = index
        _apply_selection(context, previous, names, entry.name)


def _tick():
    visible = set()
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != 'FILE_BROWSER':
                continue
            space = area.spaces.active
            operator = space.active_operator
            key = operator.as_pointer() if operator else None
            session = _sessions.get(key)
            if not session or not space.params:
                continue
            visible.add(key)
            region = next((r for r in area.regions if r.type == 'WINDOW'), None)
            if not region:
                continue
            with bpy.context.temp_override(window=window, area=area, region=region):
                if not session.opened:
                    space.params.display_type = 'THUMBNAIL'
                    bpy.ops.blender_raw.import_grid_events('INVOKE_DEFAULT')
                    session.opened = True
                if space.params.display_type == 'THUMBNAIL':
                    changed = session.refresh(space.params)
                    if session.box_rect is not None:
                        changed |= session.scroll_box(bpy.context)
                    entries = [session.entries[i] for i, *_ in session.tiles(bpy.context)]
                    if session.pump(entries) or changed:
                        area.tag_redraw()
                else:
                    session.cancel_box()
                    if session.job:
                        session.stop_worker()
    for key in _sessions.keys() - visible:
        session = _sessions[key]
        if session.opened or time.monotonic() - session.created > 15:
            _sessions.pop(key).close()
    if not _sessions:
        _remove_handler()
        return None
    pending = any(s.pending_icons for s in _sessions.values())
    dragging = any(s.box_rect is not None for s in _sessions.values())
    return .03 if pending and not dragging else .12


def open_dialog(operator):
    global _handler
    try:
        _sessions[operator.as_pointer()] = _Session()
    except OSError:  # Keep the native picker available if no scratch storage is writable.
        return
    if _handler is None:
        _handler = bpy.types.SpaceFileBrowser.draw_handler_add(_draw, (), 'WINDOW', 'POST_PIXEL')
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick)


def close_dialog(operator):
    session = _sessions.pop(operator.as_pointer(), None)
    if session:
        session.close()
        if not _sessions:
            clear()


def _remove_handler():
    global _handler
    if _handler is not None:
        bpy.types.SpaceFileBrowser.draw_handler_remove(_handler, 'WINDOW')
        _handler = None


@bpy.app.handlers.persistent
def clear(*_args):
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    _remove_handler()
    for session in _sessions.values():
        session.close()
    _sessions.clear()
