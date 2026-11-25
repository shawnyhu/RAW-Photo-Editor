# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded background jobs. Only timer callbacks touch Blender data."""
from collections import OrderedDict
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import bpy
from bpy.app.handlers import persistent
from . import catalog


_pending = OrderedDict()
_thumbnails = OrderedDict()
_active = {}
_export = None
_summary = ""
_details = []
_worker = str(Path(__file__).with_name("worker.py"))


@dataclass
class _ExportJob:
    process: subprocess.Popen
    worker_dir: tempfile.TemporaryDirectory
    snapshot_dir: tempfile.TemporaryDirectory
    report: Path
    total: int
    offset: int = 0
    results: list = field(default_factory=list)
    activity: str = ""

    def read_updates(self):
        # A worker appends newline-terminated events. Retry an incomplete line
        # on the next tick, even if it currently ends within a UTF-8 character.
        if self.report.exists():
            with self.report.open("rb") as stream:
                stream.seek(self.offset)
                for line in stream:
                    if not line.endswith(b"\n"):
                        break
                    event = json.loads(line)
                    self.offset += len(line)
                    if "stage" in event:
                        self.activity = f"{event['stage']} {event['name']}"
                    else:
                        self.results.append(event)
                        self.activity = ""


def redraw():
    from . import gallery
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if (area.type in {"IMAGE_EDITOR", "NODE_EDITOR", "FILE_BROWSER"}
                    or area.type == "PROPERTIES" and area.spaces.active.context == 'COMPOSITOR'):
                area.tag_redraw()
    gallery.redraw_statusbars()


def _params(folder, req):
    req["python_paths"] = [str(p) for p in sys.path if p and Path(p).is_dir()]
    if os.name == "nt":
        runtime = Path(bpy.app.binary_path).parent
        req["dll_paths"] = [str(runtime), str(runtime / "blender.shared")]
    req["error_path"] = str(folder / "error.txt")
    file = folder / "request.json"
    file.write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
    return file


def _start(req, blender=False):
    scratch = tempfile.TemporaryDirectory(prefix="BlenderPhotoEditor-job-")
    folder = Path(scratch.name)
    try:
        request_file = _params(folder, req)
        if blender:
            args = [bpy.app.binary_path, "--background", "--factory-startup", "--disable-autoexec",
                    "--python-exit-code", "11", "--python", _worker, "--", str(request_file)]
        else:
            # Blender exposes its bundled interpreter, including its versioned
            # filename under Blender.app/Contents/Resources on macOS.
            if not sys.executable or not Path(sys.executable).is_file():
                raise RuntimeError("Blender's bundled Python interpreter could not be found")
            args = [sys.executable, _worker, str(request_file)]
        with (folder / "worker.log").open("wb") as output:
            from . import ofx_config
            environment = os.environ.copy()
            if ofx_path := ofx_config.current_path():
                environment[ofx_config.ENVIRONMENT] = ofx_path
            process = subprocess.Popen(args, stdout=output, stderr=subprocess.STDOUT,
                                       env=environment,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return process, scratch
    except Exception:
        scratch.cleanup()
        raise


def _worker_error(scratch):
    error = Path(scratch.name) / "error.txt"
    if error.is_file():
        return error.read_text(encoding="utf-8", errors="replace").strip()
    log = (Path(scratch.name) / "worker.log").read_text(encoding="utf-8", errors="replace").strip()
    return log.splitlines()[-1][:400] if log else "The background worker exited unexpectedly"


def preview_later(photo, immediate=False):
    _pending[(photo.uid, "preview")] = time.monotonic() + (0 if immediate else 0.24)
    _pending.move_to_end((photo.uid, "preview"), last=False)
    photo.status = "Developing preview"
    photo.error = ""
    redraw()


def open_preview(photo):
    """Reuse the current Reader and recheck the linked Developer recipes on a gallery visit."""
    if photo.image and photo.decode_key and not preview_phase(photo):
        try:
            req = catalog.request(photo, "preview")
            if (req["key"] == photo.decode_key
                    and (Path(req["output"]).is_file() or catalog.packed_decode(photo, req))):
                catalog.apply_image(photo, req)
                return
        except (OSError, ValueError, RuntimeError):
            # The normal job path reports missing originals or unreadable caches.
            pass
    preview_later(photo, immediate=True)


def thumbnail_later(photo):
    if not photo.thumbnail_path or not photo.get("thumbnail_bytes"):
        _thumbnails[photo.uid] = None


def _developer_key(photo, node):
    return (photo.uid, "developer", node.id_data.session_uid, node.instance_uid)


def developer_later(photo, node):
    key = _developer_key(photo, node)
    newly_queued = key not in _pending
    _pending[key] = time.monotonic() + 0.24
    if newly_queued:
        redraw()


def preview_phase(photo, node=None):
    key = _developer_key(photo, node) if node is not None else (photo.uid, "preview")
    # A newer request may be waiting for a superseded worker to finish.
    if key in _pending:
        return "QUEUED"
    if key in _active:
        return "PROCESSING"
    return None


def developing(uid):
    return any(key[0] == uid and key[1] != "thumb" for jobs in (_pending, _active) for key in jobs)


def _developer(key, photo):
    from . import compositor
    tree = next((g for g in bpy.data.node_groups if g.session_uid == key[2]), None)
    if tree:
        for node in tree.nodes:
            if (node.bl_idname == compositor.DEVELOPER_TYPE and node.instance_uid == key[3]
                    and compositor.uses_camera_adjustments(node) and compositor.developer_source(node) == photo):
                return node
    return None


def _applied(photo, req, node=None):
    if req["mode"] == "thumb":
        path = Path(req["output"])
        photo.thumbnail_path = str(path)
        if path.stat().st_size <= 1024 * 1024:
            photo["thumbnail_bytes"] = path.read_bytes()
    elif catalog.request(photo, "preview", recipe=node)["key"] == req["key"]:
        if node is None:
            catalog.apply_image(photo, req)
        else:
            catalog.apply_developer_image(photo, node, req)


def _fail(photo, mode, exc, node=None):
    if mode == "developer":
        if node is not None:
            node.error = str(exc)
    elif mode == "preview":
        photo.status, photo.error = "Develop failed", str(exc)
    else:
        # A missing camera thumbnail must not prevent full RAW development.
        photo["thumbnail_error"] = str(exc)


def _start_preview(key):
    photo = catalog.lookup_photo(bpy.context.scene, key[0])
    if not photo:
        return
    node = None
    try:
        node = _developer(key, photo) if key[1] == "developer" else None
        if key[1] == "developer" and node is None:
            return
        req = catalog.request(photo, "preview" if node is not None else key[1], recipe=node)
        if (Path(req["output"]).is_file() or
                (key[1] != "thumb" and catalog.packed_decode(photo, req, node))):
            _applied(photo, req, node)
            redraw()
        else:
            process, scratch = _start(req)
            _active[key] = (process, scratch, req)
            if key[1] != "thumb":
                redraw()
    except Exception as exc:
        _fail(photo, key[1], exc, node)
        redraw()


def _schedule_previews():
    if len(_active) >= 2:
        return
    if _pending:
        lib = catalog.library(bpy.context)
        active_uid = lib.photos[lib.active_index].uid if 0 <= lib.active_index < len(lib.photos) else None
        # A switch promotes both the Reader and its queued Developer recipes.
        # Limit synchronous cache loads as well as concurrent subprocesses.
        now = time.monotonic()
        focused = [key for key in _pending if key[0] == active_uid and key not in _active]
        keys = sorted(focused, key=lambda key: key[1] != "preview") if focused else list(_pending)
        started = 0
        for key in keys:
            if key not in _active and _pending[key] <= now:
                del _pending[key]
                _start_preview(key)
                started += 1
                if len(_active) >= 2 or started == 2:
                    break
        # Keep the other slot free if the current recipe is still debouncing.
        if _active and any(key in _pending for key in focused):
            return
    # Leave a worker available for editing even when every thumbnail is uncached.
    # A warm Gallery can consume several on-disk thumbnails within one UI tick.
    deadline = time.monotonic() + 0.01
    for _ in range(8):
        if not _thumbnails or len(_active) >= 2 or any(key[1] == "thumb" for key in _active):
            break
        uid, _ = _thumbnails.popitem(last=False)
        _start_preview((uid, "thumb"))
        if time.monotonic() >= deadline:
            break


def tick():
    _poll_export()
    for key, (process, scratch, req) in list(_active.items()):
        if process.poll() is None:
            continue
        del _active[key]
        photo = catalog.lookup_photo(bpy.context.scene, key[0])
        node = None
        try:
            if photo:
                node = _developer(key, photo) if key[1] == "developer" else None
                if key[1] == "developer" and node is None:
                    continue
                if process.returncode:
                    if key[1] != "thumb" and req["key"] != catalog.request(photo, "preview", recipe=node)["key"]:
                        continue
                    raise RuntimeError(_worker_error(scratch))
                _applied(photo, req, node)
        except Exception as exc:
            if photo:
                _fail(photo, key[1], exc, node)
        finally:
            scratch.cleanup()
        redraw()
    _schedule_previews()
    return 0.12 if _pending or _thumbnails or _active or _export else 0.5


def export_status():
    return _summary


def export_running():
    return _export is not None


def export_activity():
    return _export.activity if _export is not None else ""


def export_progress():
    """Return processed and total photos for the running batch."""
    if _export is not None:
        return len(_export.results), _export.total
    return None


def export_details():
    return _details


def _export_bindings(photos, library):
    """Resolve paths before the snapshot moves, following the selected render graphs."""
    from . import compositor, scene_pipeline
    bindings = {}
    def binding(uid):
        if uid not in bindings:
            bindings[uid] = {"source": "", "profiles": {}, "luts": {}}
        return bindings[uid]

    for photo in photos:
        binding(photo.uid)
        for effect, node, parents in scene_pipeline.upstream(photo.scene, mode='RENDER'):
            if node.bl_idname == compositor.READER_TYPE:
                binding(node.photo_uid)
                continue
            if node.bl_idname != compositor.DEVELOPER_TYPE or node.id_data.library:
                continue
            profile = (catalog.has_custom_input(node) and node.profile_path and
                       (not node.profile_blob or node.get("embedded_profile_path") != node.profile_path))
            lut = node.look == "LUT" and node.lut_path and not node.lut_blob
            if not (profile or lut):
                continue
            # A Developer can read a different photo, also through a nested group.
            for _, reader, _ in scene_pipeline.upstream(photo.scene, node.inputs[:1], effect=effect,
                                                        parents=parents, mode='RENDER'):
                if reader.bl_idname == compositor.READER_TYPE:
                    paths = binding(reader.photo_uid)
                    if profile:
                        paths["profiles"][node.instance_uid] = str(catalog.linked_profile(node))
                    if lut:
                        paths["luts"][node.instance_uid] = str(catalog.linked_lut(node))
    for entry in library.photos:
        if entry.uid in bindings and not entry.packed:
            bindings[entry.uid]["source"] = str(catalog.linked_file(entry))
    return bindings


def start_export(context, photos, directory, fmt="", *, output_settings_scene=None):
    global _export, _summary, _details
    from . import compositor, naming
    if _export:
        raise RuntimeError("An export is already running")
    photos = tuple(photos)
    for photo in photos:
        compositor.ensure_photo_scene(context, photo)
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if fmt:
        # Script-only override. Resolve the suffix with Blender without changing
        # the user's compression, bit depth or other dependent settings.
        probe = bpy.data.scenes.new(".Blender Photo Editor Format")
        try:
            probe.render.image_settings.file_format = fmt
            ext = probe.render.file_extension
        finally:
            bpy.data.scenes.remove(probe)
    elif output_settings_scene is not None:
        if output_settings_scene.render.is_movie_format:
            raise ValueError("Choose an image format in the export settings")
        ext = output_settings_scene.render.file_extension
    else:
        ext = {}
        for photo in photos:
            render = photo.scene.render
            if render.is_movie_format:
                raise ValueError(f"Choose an image format in {photo.name}'s output settings")
            ext[photo.uid] = render.file_extension
    library = catalog.library(context)
    overwrite = library.overwrite_exports
    items = naming.export_items(photos, directory, ext, library)
    if not items:
        raise ValueError("Select at least one photo to export")
    bindings = _export_bindings(photos, library)
    snapshot_dir = tempfile.TemporaryDirectory(prefix="BlenderPhotoEditor-export-")
    try:
        folder = Path(snapshot_dir.name)
        snapshot = str(folder / "gallery.blend")
        if "FINISHED" not in bpy.ops.wm.save_as_mainfile(filepath=snapshot, copy=True, relative_remap=True):
            raise RuntimeError("Could not save an export snapshot")
        report = folder / "report.jsonl"
        req = dict(mode="export", package=__package__, format_override=fmt,
                   output_settings_scene=output_settings_scene.name if output_settings_scene else "",
                   items=items, bindings=bindings, overwrite=overwrite,
                   snapshot=snapshot, report=str(report))
        proc, proc_dir = _start(req, blender=True)
        _export = _ExportJob(proc, proc_dir, snapshot_dir, report, len(items))
        _details, _summary = [], f"Exporting 0 / {len(items)}"
        redraw()
    except Exception:
        snapshot_dir.cleanup()
        raise


def _poll_export(cancel=False):
    global _export, _summary, _details
    if not _export:
        return
    job = _export
    proc, total = job.process, job.total
    cancelled = cancel and proc.poll() is None
    if cancelled:
        proc.terminate()
        proc.wait(timeout=15)
    # Check exit first. Once the worker has exited its final report is complete.
    running = proc.poll() is None
    job.read_updates()
    records = job.results
    if running:
        label = f"Exporting {len(records)} / {total}"
        if job.activity:
            label += f"  ·  {job.activity}"
        if label != _summary:
            _summary = label
            redraw()
        return
    failed = [record for record in records if not record["ok"]]
    _details = [record["name"] + ": " + record["error"] for record in failed]
    _details.extend(record["name"] + ": " + record["warning"] for record in records if record.get("warning"))
    success = len(records) - len(failed)
    if cancelled and len(records) < total:
        _summary = f"Export cancelled. {success} / {total} saved."
    elif len(records) != total or (proc.returncode and not cancelled):
        _summary = f"Export stopped. {success} / {total} saved."
        _details.append(_worker_error(job.worker_dir))
    else:
        _summary = f"Export complete. {success} saved, {len(failed)} failed."
        if len(_details) > len(failed):
            _summary += f" {len(_details) - len(failed)} metadata warnings."
    _export = None
    job.worker_dir.cleanup()
    job.snapshot_dir.cleanup()
    redraw()


def cancel_export():
    _poll_export(cancel=True)


def stop_previews(photo_uids=None):
    """Stop thumbnail/develop work for removed photos, or all previews on file load."""
    if photo_uids is None:
        _thumbnails.clear()
    else:
        for uid in photo_uids:
            _thumbnails.pop(uid, None)
    for key in list(_pending):
        if photo_uids is None or key[0] in photo_uids:
            del _pending[key]
    for key, (process, scratch, _) in list(_active.items()):
        if photo_uids is not None and key[0] not in photo_uids:
            continue
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=15)
        scratch.cleanup()
        del _active[key]


@persistent
def _after_load(_):
    stop_previews()
    from . import compositor
    compositor.restore_preview_devices()
    catalog.migrate_apple_inputs()
    catalog.migrate_preview_settings()
    catalog.identify_legacy_caches()
    if bpy.app.background:
        return
    lib = catalog.library(bpy.context)
    for photo in lib.photos:
        thumbnail_later(photo)
    if 0 <= lib.active_index < len(lib.photos):
        photo = lib.photos[lib.active_index]
        if photo.scene:
            preview_later(photo)


def _initialize_catalog():
    _after_load(None)


def register():
    bpy.app.handlers.load_post.append(_after_load)
    if not bpy.app.background:
        bpy.app.timers.register(_initialize_catalog, first_interval=0.1)
        bpy.app.timers.register(tick, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_initialize_catalog):
        bpy.app.timers.unregister(_initialize_catalog)
    if bpy.app.timers.is_registered(tick):
        bpy.app.timers.unregister(tick)
    if _after_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_after_load)
    stop_previews()
    cancel_export()
