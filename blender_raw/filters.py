# SPDX-License-Identifier: GPL-3.0-or-later
"""Gallery filters and the capture dates stored with the project."""
from collections import deque
from datetime import date
from pathlib import Path

import bpy
from bpy.app.handlers import persistent


RAW_SUFFIXES = frozenset("dng nef nrw arw sr2 srf cr2 cr3 crw raf rw2 orf ori pef 3fr fff iiq mos mef erf x3f rwl mrw dcr kdc raw srw".split())
FORMAT_ITEMS = [("ALL", "All Formats", "Show all original file formats")] + [
    (ext.upper(), ext.upper(), f"Show .{ext} originals") for ext in sorted(RAW_SUFFIXES)
]
_pending = deque()


def _iso_date(value):
    try:
        if len(value) == 10 and date.fromisoformat(value).isoformat() == value:
            return value
    except ValueError:
        pass
    return ""


def date_choices(lib, context, edit_text):
    return sorted({p.capture_date for p in lib.photos if p.capture_date}, reverse=True)


def _date_error(lib):
    if lib.date_filter not in {"ON", "RANGE"}:
        return ""
    start = lib.date_start.strip()
    end = lib.date_end.strip() if lib.date_filter == "RANGE" else ""
    if (start and not _iso_date(start)) or (end and not _iso_date(end)):
        return "Enter dates as YYYY-MM-DD"
    if end and start and end < start:
        return "End date precedes start date"
    return ""


def filtered(lib):
    from .catalog import original_filename
    if _date_error(lib):
        return []
    needle = lib.query.strip().casefold()
    start, end = lib.date_start.strip(), lib.date_end.strip()
    result = []
    for i, photo in enumerate(lib.photos):
        if photo.rating < lib.min_rating or (lib.flag_filter != "ALL" and photo.flagged != (lib.flag_filter == "FLAGGED")):
            continue
        if lib.format_filter != "ALL" and Path(original_filename(photo)).suffix[1:].upper() != lib.format_filter:
            continue
        if needle and needle not in (photo.name + " " + photo.tags).casefold():
            continue
        if bpy.app.background and lib.date_filter != "ALL" and not photo.capture_date_indexed:
            # Background callers have no event loop to run the migration timer.
            index_photo(photo)
        captured = photo.capture_date
        if lib.date_filter == "UNKNOWN" and (captured or not photo.capture_date_indexed):
            continue
        if lib.date_filter == "ON" and start and captured != start:
            continue
        if lib.date_filter == "RANGE" and (start or end):
            if not captured or (start and captured < start) or (end and captured > end):
                continue
        result.append((i, photo))
    return result


def draw(layout, lib):
    layout.prop(lib, "format_filter")
    layout.prop(lib, "date_filter")
    if lib.date_filter in {"ON", "RANGE"}:
        layout.prop(lib, "date_start", text="Date" if lib.date_filter == "ON" else "From")
        if lib.date_filter == "RANGE":
            layout.prop(lib, "date_end", text="Through")
        error = _date_error(lib)
        if error:
            layout.label(text=error, icon="ERROR")
    if lib.date_filter != "ALL" and _pending:
        layout.label(text="Reading capture dates…", icon="TIME")


def index_photo(photo, path=None):
    from . import catalog, metadata
    try:
        info = metadata.capture_info(path or catalog.source_file(photo))
        photo["capture_info"] = info
        photo.capture_date = info["captured"][:10]
        if "retry_capture_date" in photo:
            del photo["retry_capture_date"]
    except (OSError, RuntimeError, ValueError, ImportError, OverflowError):
        # Retain cached information for a missing original, but never show a
        # previous file's camera information after an explicit relink.
        if path is not None:
            photo.capture_date = ""
            if "capture_info" in photo:
                del photo["capture_info"]
        photo["retry_capture_date"] = True
    photo.capture_date_indexed = True


def index_imported_photo(photo, index, path):
    if bpy.app.background:
        index_photo(photo, path)
        return
    photo.capture_date, photo.capture_date_indexed = "", False
    _pending.append((photo.id_data.name, index, photo.uid))
    _start_index()


def _index_next():
    if not _pending:
        return None
    scene_name, idx, uid = _pending.popleft()
    scene = bpy.data.scenes.get(scene_name)
    if scene and not scene.library:
        photos = scene.blender_raw.photos
        photo = photos[idx] if idx < len(photos) and photos[idx].uid == uid else next((p for p in photos if p.uid == uid), None)
        if photo and ("capture_info" not in photo or not photo.capture_date_indexed or photo.get("retry_capture_date")):
            index_photo(photo)
            from . import jobs
            jobs.redraw()
    return .025 if _pending else None


def _start_index():
    if _pending and not bpy.app.timers.is_registered(_index_next):
        bpy.app.timers.register(_index_next, first_interval=.05)


@persistent
def index_saved_photos(*_):
    _pending.clear()
    if bpy.app.background:
        return
    for scene in bpy.data.scenes:
        if not scene.library:
            _pending.extend((scene.name, i, p.uid) for i, p in enumerate(scene.blender_raw.photos)
                            if "capture_info" not in p or not p.capture_date_indexed or p.get("retry_capture_date"))
    _start_index()


def register():
    bpy.app.handlers.load_post.append(index_saved_photos)
    if not bpy.app.background:
        bpy.app.timers.register(index_saved_photos, first_interval=.1)


def unregister():
    bpy.app.handlers.load_post.remove(index_saved_photos)
    for callback in (index_saved_photos, _index_next):
        if bpy.app.timers.is_registered(callback):
            bpy.app.timers.unregister(callback)
    _pending.clear()
