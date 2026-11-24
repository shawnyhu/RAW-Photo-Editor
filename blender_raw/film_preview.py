# SPDX-License-Identifier: GPL-3.0-or-later
"""Debounced whole-frame film previews from an isolated copy of the user's graph."""
from collections import defaultdict, OrderedDict
from dataclasses import dataclass
from hashlib import file_digest, sha256
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import time

import bpy
from bpy.app.handlers import persistent

from . import catalog, film_bake, jobs, film_node, scene_pipeline


@dataclass
class PreviewJob:
    key: tuple
    process: object
    worker_dir: TemporaryDirectory
    snapshot_dir: TemporaryDirectory
    signature: tuple = ()
    cancelled: bool = False

    def cancel(self):
        self.cancelled = True
        if self.process.poll() is None:
            self.process.kill()


_pending = OrderedDict()
_seen = {}
_job = None

_WAITING_RAW = "Waiting for RAW preview"
_CACHE_SIGNATURE = 'photo_editor_film_preview_v2'


def _key(node):
    return node.id_data.session_uid, node.instance_uid


def preview_status(node):
    """Read the film worker state for the compositor overlay without scheduling work."""
    if not node.live_preview:
        if node.mute:
            return "IDLE", "Muted"
        scan = node.node_tree.nodes.get("Film Result") if node.node_tree else None
        if scan and scan.image:
            return "IDLE", "Paused · Holding last scan"
        return "IDLE", "Preview paused"
    error = film_node.backend_error(node)
    if error:
        return "ERROR", error
    if node.mute:
        return "IDLE", "Live preview · Muted"
    if not node.inputs or not film_bake.active_input_links(node.inputs[0]):
        return "IDLE", "Live preview · Waiting for input"
    key = _key(node)
    if key in _pending:
        return "QUEUED", "Live preview · Queued"
    if _job and _job.key == key and not _job.cancelled:
        return "PROCESSING", "Live preview · Rendering"
    if node.status == "Ready":
        return "READY", "Live preview · Ready"
    if node.status == _WAITING_RAW:
        return "IDLE", "Live preview · " + _WAITING_RAW
    if node.status and node.status != "Rendering film preview":
        return "ERROR", "Live preview · Error · " + node.status
    return "IDLE", "Live preview · Waiting"


def request(node):
    """Called by node buttons/properties. Defer file and RNA writes to the timer."""
    if not bpy.app.background and node.instance_uid and node.live_preview and not film_node.backend_error(node):
        _pending[_key(node)] = time.monotonic() + 0.35


def live_preview_changed(node):
    if not node.instance_uid:
        return
    key = _key(node)
    _seen.pop(key, None)
    if node.live_preview and not film_node.backend_error(node):
        _pending[key] = time.monotonic()
    else:
        _pending.pop(key, None)
        if _job and _job.key == key:
            _job.cancel()


def _values(owner, skip=()):
    """Capture editable scalar/array settings (including socket defaults)."""
    values = []
    for prop in owner.bl_rna.properties:
        name = prop.identifier
        if prop.is_readonly or name in skip or prop.type not in {"BOOLEAN", "INT", "FLOAT", "STRING", "ENUM"}:
            continue
        value = getattr(owner, name)
        if getattr(prop, "is_array", False):
            value = tuple(value)
        elif isinstance(value, set):
            value = tuple(sorted(value))
        values.append(value)
    return tuple(values)


_DECORATION = {"name", "label", "location", "location_absolute", "width", "height", "color", "select",
               "show_options", "show_preview", "show_texture", "hide", "use_custom_color",
               "show_expanded", "warning_propagation", "photo_editor_expanded"}
_TRACKED_NODE_DATA = {p.identifier for p in bpy.types.Node.bl_rna.properties} | {
    'node_tree', 'image', 'image_user', 'color_ramp', 'mapping'}


def _signature(node, scene, parents=()):
    """Track the connected branch; never include a film node's own cached result."""
    from . import compositor, film_node
    visited = set()
    missing_raw = False
    reusable = True
    owner_effect, owner_parents = scene_pipeline.instance_for_node(scene, node, 'PREVIEW')
    parents = parents or owner_parents
    state = [(sys.platform, bpy.app.version, scene.frame_current_final, scene.render.resolution_x,
              scene.render.resolution_y, scene.render.resolution_percentage, bpy.data.colorspace.working_space,
              scene.get('blenderraw_photo_id', scene.name))]

    def visit(n, path, output=None, effect=None):
        nonlocal missing_raw, reusable
        token = (effect.as_pointer() if effect else 0, n.as_pointer(), tuple(group.as_pointer() for group in path),
                 output.identifier if output else None)
        if token in visited:
            return
        visited.add(token)
        state.append((tuple(group.name for group in path), n.name, n.bl_idname, n.mute,
                      output.identifier if output else None))
        if n.id_data.animation_data:
            reusable = False
        if n.type == 'GROUP_INPUT' and path and output:
            outer = next((s for s in path[-1].inputs if s.identifier == output.identifier), None)
            if outer:
                visit_socket(outer, path[:-1], effect)
            return
        if n.type == 'GROUP_INPUT':
            if effect and output:
                if output.identifier == scene_pipeline.first_input(effect.node_group):
                    previous, socket = scene_pipeline.previous_output(scene, effect)
                    if socket:
                        state.append(('effect_input', effect.name, previous.name))
                        visit_socket(socket, (), previous)
                    else:
                        reusable = False  # The first effect can read the rendered scene.
                else:
                    value = scene_pipeline.effect_input(effect, output.identifier)
                    state.append(('effect_parameter', effect.name, output.identifier, scene_pipeline._freeze(value)))
                    if value is None or isinstance(value, bpy.types.ID):
                        reusable = False
            else:
                reusable = False
            return
        if n.mute and output:
            for link in n.internal_links:
                if link.to_socket == output:
                    visit_socket(link.from_socket, path, effect)
            return
        if n.bl_idname in film_node.FILM_TYPES:
            if n != node and not n.live_preview:
                cached = n.node_tree.nodes["Film Result"].image
                state.append(("paused", cached.get('photo_editor_decode_cache') if cached else None))
                if cached and not cached.get('photo_editor_decode_cache'):
                    reusable = False
                if not cached:
                    visit_inputs(n, path, effect)
                return
            state.append(tuple(film_node.recipe(n).items()))
            if n.bl_idname == film_node.OFX_TYPE:
                from . import ofx_config, spektrafilm_ofx_engine
                state.append(spektrafilm_ofx_engine.cache_identity(ofx_config.current_path()))
        else:
            skip = _DECORATION
            # Assets such as masks, movie clips, textures and render layers have external
            # inputs beyond the socket graph. Only reuse scans whose dependencies we track.
            if n.bl_idname not in {compositor.READER_TYPE, compositor.DEVELOPER_TYPE} and any(
                    p.identifier not in _TRACKED_NODE_DATA and p.type in {'POINTER', 'COLLECTION'}
                    for p in n.bl_rna.properties):
                reusable = False
            if n.bl_idname == "CompositorNodeImage":
                # Blender warns when querying a dynamic enum which has no items.
                if n.image is None or n.image.type != "MULTILAYER":
                    skip = skip | {"layer"}
                if n.image is None or not n.image.is_multiview:
                    skip = skip | {"view"}
            state.append(_values(n, skip))
            # Value and RGB nodes keep their user-editable value on the output.
            for socket in n.outputs:
                if n.type in {"VALUE", "RGB"} and hasattr(socket, "default_value"):
                    state.append(_values(socket))
            image = getattr(n, "image", None)
            raw_node = n.bl_idname == 'CompositorNodeImage' and path and (
                (path[-1].bl_idname == compositor.READER_TYPE and n.name == 'Decoded Image') or
                (path[-1].bl_idname == compositor.DEVELOPER_TYPE and n.name in {'Developed RAW', 'As Shot RAW'}))
            if raw_node and image is None:
                missing_raw = True
            if image is not None:
                cache_key = image.get('photo_editor_decode_cache')
                state.append((cache_key, image.source, image.colorspace_settings.name, image.alpha_mode))
                if image.source != 'FILE' or image.is_dirty or image.use_multiview or image.type != 'IMAGE':
                    state.append(image.session_uid)
                    reusable = False
                elif image.packed_file:
                    if not cache_key:
                        # Repacking an arbitrary asset can replace its contents without changing its name.
                        reusable = False
                    state.append((image.packed_file.size, cache_key or image.packed_file.as_pointer()))
                else:
                    source = Path(os.path.normpath(bpy.path.abspath(image.filepath, library=image.library)))
                    state.append(str(source))
                    try:
                        stat = source.stat()
                        state.append((stat.st_mtime_ns, stat.st_size))
                        if raw_node and not stat.st_size:
                            missing_raw = True
                    except OSError:
                        reusable = False
                        if raw_node:
                            missing_raw = True
                if hasattr(n, "image_user"):
                    state.append(_values(n.image_user))
            ramp = getattr(n, "color_ramp", None)
            if ramp:
                state.append((_values(ramp), tuple((e.position, tuple(e.color)) for e in ramp.elements)))
            mapping = getattr(n, "mapping", None)
            if mapping:
                state.append(_values(mapping))
                if hasattr(mapping, "curves"):
                    state.append(tuple((_values(curve), tuple((tuple(p.location), p.handle_type) for p in curve.points))
                                       for curve in mapping.curves))
            child = getattr(n, "node_tree", None)
            if child:
                for end in child.nodes:
                    if end.type == "GROUP_OUTPUT" and end.is_active_output:
                        for socket in end.inputs:
                            if output is None or socket.identifier == output.identifier:
                                visit_socket(socket, (*path, n), effect)
                return
        visit_inputs(n, path, effect)

    def visit_socket(socket, path, effect):
        links = film_bake.active_input_links(socket)
        for link in links:
            state.append((socket.identifier, link.from_node.name, link.from_socket.identifier))
            visit(link.from_node, path, link.from_socket, effect)
        if not links and hasattr(socket, "default_value"):
            state.append(_values(socket))

    def visit_inputs(n, path, effect):
        if n.bl_idname == 'CompositorNodeSwitch' and not film_bake.active_input_links(n.inputs['Switch']):
            visit_socket(n.inputs['On' if n.inputs['Switch'].default_value else 'Off'], path, effect)
            return
        for socket in n.inputs:
            if socket.enabled:
                visit_socket(socket, path, effect)

    visit(node, parents, effect=owner_effect)
    return (sha256(repr(state).encode('utf-8')).hexdigest(), reusable), missing_raw


def _active_nodes():
    from . import film_node
    found = {}
    for window in bpy.context.window_manager.windows:
        scene = window.scene
        for _effect, node, parents in scene_pipeline.node_instances(scene):
            if node.bl_idname in film_node.FILM_TYPES and not node.id_data.library:
                found.setdefault(_key(node), (scene, node, parents))
    return found


def _clear_result(node):
    from . import film_node
    cached = node.node_tree.nodes["Film Result"].image
    film_node.invalidate(node)
    if cached is not None and cached.get("photo_editor_decode_cache") and not cached.users and not cached.library:
        bpy.data.images.remove(cached)


def discard_scan(node):
    key = _key(node)
    if _job and _job.key == key:
        _job.cancel()
    _seen.pop(key, None)
    _pending.pop(key, None)
    cached = node.node_tree.nodes.get('Film Result') if node.node_tree else None
    if cached and cached.image:
        _clear_result(node)
    if _CACHE_SIGNATURE in node:
        del node[_CACHE_SIGNATURE]
    if node.status:
        node.status = ''


def _animated_exposures(tree, films):
    animation = tree.animation_data
    if not animation:
        return set()
    targets = {node.path_from_id(prop): node for node in films for prop in ('exposure', 'print_exposure')}
    affected = {targets[c.data_path] for c in animation.drivers if c.data_path in targets}
    actions = {(animation.action, animation.action_slot_handle)}
    if animation.action_tweak_storage:
        actions.add((animation.action_tweak_storage, animation.action_slot_handle_tweak_storage))
    strips = [strip for track in animation.nla_tracks for strip in track.strips]
    while strips:
        strip = strips.pop()
        if strip.action:
            actions.add((strip.action, strip.action_slot_handle))
        strips.extend(strip.strips)
    for action, slot in actions:
        if not action:
            continue
        for layer in action.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    if bag.slot_handle != slot:
                        continue
                    for curve in bag.fcurves:
                        if curve.data_path not in targets:
                            continue
                        if (curve.modifiers or any(p.co.y for p in curve.sampled_points)
                                or any(p.co.y or p.handle_left.y or p.handle_right.y for p in curve.keyframe_points)):
                            affected.add(targets[curve.data_path])
    return affected


@persistent
def migrate_exposure_caches(*_args):
    """Discard scans baked with the old Film/Print Exposure controls, including their consumers."""
    from . import film_bake, film_node
    trees = [tree for tree in bpy.data.node_groups if tree.bl_idname == 'CompositorNodeTree']
    film_by_tree = {tree: [n for n in tree.nodes if n.bl_idname in film_node.FILM_TYPES] for tree in trees}
    film_nodes = [node for group in film_by_tree.values() for node in group]
    legacy = {node for node in film_nodes
              if node.bl_idname == film_node.FILM_TYPE and film_node.has_legacy_exposure(node)}
    for tree, films in film_by_tree.items():
        lite = [node for node in films if node.bl_idname == film_node.FILM_TYPE]
        if lite:
            legacy.update(_animated_exposures(tree, lite))
    if not legacy:
        return

    parents = defaultdict(list)
    for tree in trees:
        for group in tree.nodes:
            if group.bl_idname == 'CompositorNodeGroup' and group.node_tree:
                parents[group.node_tree].append(group)

    def instance_paths(tree):
        if tree not in parents:
            yield ()
        for group in parents.get(tree, ()):
            for path in instance_paths(group.id_data):
                yield (*path, group)

    # A group can have several instances but only one cached image on its film node.
    consumers = defaultdict(set)
    for node in film_nodes:
        for path in instance_paths(node.id_data):
            for source, _ in film_bake.upstream(node.inputs, path):
                if source.bl_idname in film_node.FILM_TYPES:
                    consumers[source].add(node)
    affected = set(legacy)
    pending = list(legacy)
    while pending:
        downstream = consumers[pending.pop()] - affected
        affected.update(downstream)
        pending.extend(downstream)

    for node in affected:
        if node in legacy:
            film_node.reset_legacy_exposure(node)
        cached = node.node_tree.nodes.get('Film Result') if node.node_tree else None
        if cached and cached.image and cached.image.get('spektrafilm_neutral_exposure'):
            # Old actions/linked originals may remain in the project after a new scan.
            continue
        discard_scan(node)


def _start(key, scene, node):
    from . import film_bake, film_node
    scratch = TemporaryDirectory(prefix="BlenderPhotoEditor-film-")
    folder = Path(scratch.name)
    try:
        result = str(folder / "result.exr")
        direct = film_bake.direct_raw_input(scene, node, folder)
        if direct:
            source, exposure = direct
            recipe = {**film_node.recipe(node), 'frame': scene.frame_current + scene.frame_subframe}
            req = dict(mode="film_raw_preview", source=str(source), exposure=exposure, recipe=recipe, output=result)
        else:
            snapshot = str(folder / "preview.blend")
            if "FINISHED" not in bpy.ops.wm.save_as_mainfile(filepath=snapshot, copy=True, relative_remap=True):
                raise RuntimeError("Could not snapshot the compositor for Spektrafilm")
            req = dict(mode="film_preview", package=__package__, snapshot=snapshot,
                       scene=scene.name, tree=node.id_data.name, node=node.name, output=result)
        process, worker_dir = jobs._start(req, blender=direct is None)
        return PreviewJob(key, process, worker_dir, scratch, _seen[key])
    except Exception:
        scratch.cleanup()
        raise


def _finish(job, node):
    from . import film_node
    if job.process.returncode:
        raise RuntimeError(jobs._worker_error(job.worker_dir))
    result = Path(job.snapshot_dir.name) / "result.exr"
    with result.open("rb") as stream:
        key = file_digest(stream, "sha256").hexdigest()
    dest = catalog.cache_dir() / (key + ".exr")
    intact = False
    if dest.is_file() and dest.stat().st_size == result.stat().st_size:
        with dest.open('rb') as stream:
            intact = file_digest(stream, 'sha256').hexdigest() == key
    if not intact:
        result.replace(dest)
    image = catalog._cached_image(dict(key=key, output=str(dest)))
    image.alpha_mode = "PREMUL"
    image['spektrafilm_neutral_exposure'] = True
    previous = node.node_tree.nodes["Film Result"].image
    film_node.apply_result(node, image)
    if previous and previous != image and previous.get("photo_editor_decode_cache") and not previous.users and not previous.library:
        bpy.data.images.remove(previous)
    node.status = "Ready"
    return image


def _tick():
    global _job
    active = _active_nodes()
    now = time.monotonic()
    redraw = False
    for key in _seen.keys() - active.keys():
        del _seen[key]
    for key in _pending.keys() - active.keys():
        del _pending[key]
    for key, (scene, node, parents) in active.items():
        if error := film_node.backend_error(node):
            _pending.pop(key, None)
            _seen.pop(key, None)
            if node.status != error:
                node.status = error
                redraw = True
            continue
        cache_node = node.node_tree.nodes.get('Film Result') if node.node_tree else None
        cached = cache_node.image if cache_node else None
        if (cached and not cached.has_data and not cached.packed_file
                and not Path(bpy.path.abspath(cached.filepath, library=cached.library)).is_file()):
            _clear_result(node)
            cached = None
            _seen.pop(key, None)
            node.status = ''
            redraw = True
        if not node.live_preview:
            _seen.pop(key, None)
            _pending.pop(key, None)
            continue
        if node.mute or not node.inputs or not film_bake.active_input_links(node.inputs[0]):
            _seen.pop(key, None)
            if node.node_tree and node.node_tree.nodes["Film Result"].image:
                _clear_result(node)
            if node.status:
                node.status = ""
                redraw = True
            _pending.pop(key, None)
            continue
        try:
            signature, missing_raw = _signature(node, scene, parents)
        except ValueError as exc:
            node.status = str(exc)
            _pending.pop(key, None)
            _seen.pop(key, None)
            continue
        if missing_raw:
            _seen.pop(key, None)
            _pending.pop(key, None)
            if node.status != _WAITING_RAW:
                node.status = _WAITING_RAW
                redraw = True
            continue
        changed = signature != _seen.get(key)
        if changed or key in _pending:
            # Loading a graph can queue the same request as an edit. Check the actual inputs
            # and bound result before launching a worker for either kind of notification.
            stamp = signature[0] + ':' + str(cached.get('photo_editor_decode_cache')) if cached else None
            if signature[1] and stamp and node.get(_CACHE_SIGNATURE) == stamp and catalog.ensure_cache_pixels(cached):
                _pending.pop(key, None)
                node.status = 'Ready'
                redraw = True
            elif changed and (key in _seen or key not in _pending):
                _pending[key] = now + 0.35
            _seen[key] = signature
            redraw |= changed

    previous_job = _job
    if _job and not _job.cancelled and (_job.signature != _seen.get(_job.key) or _job.key in _pending):
        _job.cancel()
    if _job and _job.process.poll() is not None:
        job, _job = _job, None
        try:
            if not job.cancelled and job.signature == _seen.get(job.key) and job.key not in _pending and job.key in active:
                node = active[job.key][1]
                try:
                    image = _finish(job, node)
                    if job.signature[1]:
                        node[_CACHE_SIGNATURE] = job.signature[0] + ':' + image['photo_editor_decode_cache']
                    elif _CACHE_SIGNATURE in node:
                        del node[_CACHE_SIGNATURE]
                except Exception as exc:
                    node.status = str(exc) or "Film preview failed"
        finally:
            job.worker_dir.cleanup()
            job.snapshot_dir.cleanup()
    if _job is None and not bpy.app.is_job_running("RENDER"):
        for key, due in list(_pending.items()):
            if due > now:
                continue
            scene, node, _ = active[key]
            uid = scene.get("blenderraw_photo_id")
            if uid and jobs.developing(uid):
                continue
            del _pending[key]
            redraw = True
            try:
                # Keep the last scan and its display view while the replacement renders.
                node.status = "Rendering film preview"
                _job = _start(key, scene, node)
            except Exception as exc:
                node.status = str(exc) or "Film preview failed"
            break
    if redraw or _job is not previous_job:
        jobs.redraw()
    if _job or _pending:
        return 0.15
    return 0.45 if active else 1.0


@persistent
def _stop(*_args):
    global _job
    _pending.clear()
    _seen.clear()
    if _job:
        _job.cancel()
        _job.process.wait()
        _job.worker_dir.cleanup()
        _job.snapshot_dir.cleanup()
        _job = None


def register():
    bpy.app.handlers.load_post.append(migrate_exposure_caches)
    bpy.app.handlers.blend_import_post.append(migrate_exposure_caches)
    if hasattr(bpy.data, 'node_groups'):
        migrate_exposure_caches()
    else:
        # An add-on enabled inside an open file receives restricted bpy.data during registration.
        bpy.app.timers.register(migrate_exposure_caches, first_interval=0.0)
    if not bpy.app.background:
        bpy.app.handlers.load_pre.append(_stop)
        bpy.app.timers.register(_tick, first_interval=0.5, persistent=True)


def unregister():
    for handlers in (bpy.app.handlers.load_post, bpy.app.handlers.blend_import_post):
        if migrate_exposure_caches in handlers:
            handlers.remove(migrate_exposure_caches)
    if bpy.app.timers.is_registered(migrate_exposure_caches):
        bpy.app.timers.unregister(migrate_exposure_caches)
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    if _stop in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_stop)
    _stop()
