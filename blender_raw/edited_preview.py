# SPDX-License-Identifier: GPL-3.0-or-later
"""Frame the selected photo in the Viewer and capture its color-managed thumbnail."""

import hashlib
import os
from pathlib import Path
import tempfile

import bpy
from bpy.app.handlers import persistent
import numpy as np

from . import catalog, compositor, jobs


_started = {}
_finished = None
_fit_requests = {}


def fit_on_switch(context, scene, uid):
    if bpy.app.background:
        return
    for window in context.window_manager.windows:
        if (window == context.window or window.scene == scene) and any(a.type == 'IMAGE_EDITOR' for a in window.screen.areas):
            _fit_requests[window.as_pointer()] = (window.screen.session_uid, scene.session_uid, uid, False)
    _schedule_fit()


def _schedule_fit():
    if _fit_requests and not bpy.app.timers.is_registered(_fit_views):
        bpy.app.timers.register(_fit_views, first_interval=.05)


def _fit_views():
    windows = {w.as_pointer(): w for w in bpy.context.window_manager.windows}
    busy = bpy.app.is_job_running("COMPOSITE")
    retry = False
    for key, (screen_id, scene_id, uid, composited) in list(_fit_requests.items()):
        window = windows.get(key)
        if (not window or window.screen.session_uid != screen_id or window.scene.session_uid != scene_id
                or window.scene.get(compositor.PHOTO_TAG) != uid):
            del _fit_requests[key]
            continue
        scene = window.scene
        photo = catalog.lookup_photo(scene, uid)
        viewers = [a for a in window.screen.areas if a.type == 'IMAGE_EDITOR'
                   and a.spaces.active.image and a.spaces.active.image.type == 'COMPOSITING']
        if not viewers or not photo or photo.error:
            del _fit_requests[key]
            continue
        if photo.status != 'Ready' or not photo.image or busy:
            retry |= busy or jobs.developing(uid)
            continue
        size = tuple(viewers[0].spaces.active.image.size)
        # Until the new image arrives, Blender can still expose the previous
        # photo's canvas. Edited graphs may produce a custom-sized Viewer.
        if min(size) <= 0 or (not composited and size != (scene.render.resolution_x, scene.render.resolution_y)):
            continue
        for area in viewers:
            region = next((r for r in area.regions if r.type == 'WINDOW'), None)
            if region:
                with bpy.context.temp_override(window=window, area=area, region=region):
                    if bpy.ops.image.view_all.poll():
                        bpy.ops.image.view_all(fit_view=True)
        del _fit_requests[key]
    return .15 if retry else None


def _signature(scene):
    from . import scene_pipeline
    if not scene or not scene.get(compositor.STUDIO_TAG):
        return None
    uid = scene.get(compositor.PHOTO_TAG)
    photo = catalog.lookup_photo(scene, uid)
    if not photo or not photo.image or not photo.decode_key or photo.status != 'Ready' or jobs.developing(uid):
        return None
    # An explicit Viewer is a debugging view, not the final photograph.
    active = scene.compositor_effects.active
    if active:
        for effect, node, _ in scene_pipeline.node_instances(scene):
            if effect == active and node.bl_idname == 'CompositorNodeViewer' and not node.mute:
                return None
    return (scene.as_pointer(), uid, scene_pipeline.stack_signature(scene), photo.decode_key)


@persistent
def _pre(scene, *_):
    global _finished
    _finished = None
    _started[scene.as_pointer()] = (scene.get(compositor.PHOTO_TAG), _signature(scene))


@persistent
def _cancel(scene, *_):
    _started.pop(scene.as_pointer(), None)


@persistent
def _post(scene, *_):
    global _finished
    uid, token = _started.pop(scene.as_pointer(), (None, None))
    if uid and uid == scene.get(compositor.PHOTO_TAG):
        for key, request in list(_fit_requests.items()):
            if request[1:3] == (scene.session_uid, uid):
                _fit_requests[key] = (*request[:3], True)
                _schedule_fit()
    if token and token == _signature(scene):
        _finished = token
        if not bpy.app.timers.is_registered(_commit):
            bpy.app.timers.register(_commit, first_interval=.35)


def _read_small(viewer, width, height):
    # Read the completed preview. This does not request a compositor render.
    # gpu.texture.from_image does not expose the actual Viewer image on all backends.
    data = np.empty(width * height * 4, dtype=np.float32)
    viewer.pixels.foreach_get(data)
    source = data.reshape(height, width, 4)
    scale = min(1, 240 / max(width, height))
    w, h = max(1, round(width * scale)), max(1, round(height * scale))
    xs = np.arange(w)
    ys = np.arange(h)
    small = np.zeros((h, w, 4), dtype=np.float32)
    for offset_y in (.25, .75):
        y = ((ys + offset_y) * height / h).astype(int)
        for offset_x in (.25, .75):
            x = ((xs + offset_x) * width / w).astype(int)
            small += source[y[:, None], x]
    return (w, h), (small * .25).ravel()


def _commit():
    global _finished
    token, _finished = _finished, None
    if not token or bpy.app.is_job_running("COMPOSITE"):
        return
    scene = next((s for s in bpy.data.scenes if s.as_pointer() == token[0]), None)
    if not scene or token != _signature(scene):
        return
    viewer = next((i for i in bpy.data.images if i.type == 'COMPOSITING'), None)
    if not viewer or not viewer.has_data:
        return
    width, height = viewer.size
    # Bound main-thread memory and latency with unrelated full-size Viewer results.
    if width < 1 or height < 1 or width * height > 12_000_000:
        return
    size, pixels = _read_small(viewer, width, height)
    # An empty Viewer can complete during a photo switch. Keep the last visible
    # thumbnail (or the camera thumbnail) instead of baking transparency to black.
    empty = not np.any(pixels[3::4] > 0)
    compositor.check_viewer_pixels(scene, viewer, empty=empty)
    if empty:
        return
    photo = catalog.lookup_photo(scene, token[1])
    image = bpy.data.images.new(".Blender Photo Editor Thumbnail", *size, alpha=True, float_buffer=True)
    settings = scene.render.image_settings
    thumb_scene = scene
    try:
        if (settings.file_format not in {"PNG", "JPEG"} or settings.color_management != "FOLLOW_SCENE"
                or settings.color_mode == "BW"):
            # Preserve the Viewer's display transform even when a different
            # format or color space has been selected for final delivery.
            thumb_scene = scene.copy()
            thumbnail_format = thumb_scene.render.image_settings
            thumbnail_format.media_type = "IMAGE"
            thumbnail_format.file_format = "PNG"
            thumbnail_format.color_mode = "RGB"
            thumbnail_format.color_depth = "8"
            thumbnail_format.color_management = "FOLLOW_SCENE"
        suffix = thumb_scene.render.file_extension
        image.pixels.foreach_set(pixels)
        image.update()
        with tempfile.TemporaryDirectory(dir=catalog.cache_dir()) as folder:
            path = Path(folder) / ('preview'+suffix)
            image.save_render(str(path), scene=thumb_scene)
            payload = path.read_bytes()
            target = catalog.cache_dir() / ('edited-'+hashlib.sha256(payload).hexdigest()+suffix)
            if not target.is_file():
                os.replace(path, target)
        if photo.get("edited_thumbnail_path") != str(target):
            photo["edited_thumbnail_path"] = str(target)
            photo["edited_thumbnail_bytes"] = payload
            for win in bpy.context.window_manager.windows:
                for area in win.screen.areas:
                    if area.type == 'NODE_EDITOR':
                        area.tag_redraw()
    finally:
        if thumb_scene != scene:
            bpy.data.scenes.remove(thumb_scene)
        bpy.data.images.remove(image)


@persistent
def _clear(*_):
    global _finished
    _started.clear()
    _fit_requests.clear()
    _finished = None


_handlers = ((bpy.app.handlers.composite_pre, _pre),
             (bpy.app.handlers.composite_cancel, _cancel),
             (bpy.app.handlers.composite_post, _post),
             (bpy.app.handlers.load_pre, _clear))


def register():
    for handler, callback in _handlers:
        handler.append(callback)


def unregister():
    if bpy.app.timers.is_registered(_fit_views):
        bpy.app.timers.unregister(_fit_views)
    if bpy.app.timers.is_registered(_commit):
        bpy.app.timers.unregister(_commit)
    for handler, callback in _handlers:
        handler.remove(callback)
    _clear()
