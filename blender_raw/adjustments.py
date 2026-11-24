# SPDX-License-Identifier: GPL-3.0-or-later
"""Gallery clipboard for independent native Scene Effects.

The clipboard is a private .blend snapshot plus plain Python interface values.
It survives Undo without holding live RNA references and never uses Blender's
node clipboard. Each paste owns a fresh set of mutable node groups.
"""
from collections import Counter
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
import uuid

import bpy
from bpy.app.handlers import persistent

from . import catalog, compositor, film_node, film_preview


@dataclass
class EffectSnapshot:
    name: str
    group_name: str
    settings: dict
    properties: dict


@dataclass
class Clipboard:
    directory: TemporaryDirectory
    source_uid: str
    effects: list
    active_index: int


@dataclass
class PastePlan:
    scene: bpy.types.Scene
    reader: object
    old_effects: list
    staging: bpy.types.Scene


_clipboard = None
# NodeTree.copy holds Main's lock. Node.copy callbacks must borrow their child
# groups, which are copied explicitly after the outer copy returns.
_copying_tree = ContextVar("photo_adjustment_tree_copy", default=False)
_EXTERNAL = "blenderraw_clipboard_external_ids"
_SETTINGS = ("enable_for_render", "enable_for_preview", "show_expanded", "show_node_group_selector")


def borrowing_groups():
    return _copying_tree.get()


@persistent
def clear(*_):
    global _clipboard
    if _clipboard:
        _clipboard.directory.cleanup()
        _clipboard = None


def has_targets(photos):
    return _clipboard is not None and any(p.uid != _clipboard.source_uid for p in photos)


def _groups(tree):
    seen = set()
    pending = [tree]
    while pending:
        group = pending.pop()
        if not group or group in seen:
            continue
        seen.add(group)
        yield group
        pending.extend(n.node_tree for n in group.nodes if getattr(n, "node_tree", None))


def _reader_effect(scene):
    if not scene or not scene.is_editable:
        raise ValueError("The photo Scene is missing or read-only")
    effects = list(scene.compositor_effects)
    readers = [(effect, node) for effect in effects
               for tree in _groups(effect.node_group)
               for node in tree.nodes if node.bl_idname == compositor.READER_TYPE]
    if len(readers) != 1 or readers[0][0] != (effects[0] if effects else None):
        raise ValueError(f"{scene.name}: keep one Raw Reader in the first effect before copying adjustments")
    if any(effect.node_group is None for effect in effects):
        raise ValueError(f"{scene.name}: an effect has no node group")
    return readers[0][0]


def _encode(value):
    if isinstance(value, bpy.types.ID):
        return {"id": value.session_uid}
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return tuple(value)


def _decode(value, ids):
    if isinstance(value, dict) and "id" in value:
        if value["id"] not in ids:
            raise ValueError("A copied effect input is no longer in this project")
        return ids[value["id"]]
    return value


def _interface_state(owner):
    """Capture generated inputs/panels RNA, including menu values and ID inputs."""
    if owner is None:
        return {}
    state = {}
    for prop in owner.bl_rna.properties:
        name = prop.identifier
        if name in {"rna_type", "name"}:
            continue
        value = getattr(owner, name)
        if prop.type == "POINTER" and value is not None and not isinstance(value, bpy.types.ID):
            children = _interface_state(value)
            if children:
                state[name] = {"children": children}
        elif not prop.is_readonly and prop.type != "COLLECTION":
            state[name] = {"value": _encode(value)}
    return state


def _restore_interface(owner, state, ids):
    for name, record in state.items():
        if not hasattr(owner, name):
            raise ValueError(f"The copied effect input {name} is unavailable")
        if "children" in record:
            _restore_interface(getattr(owner, name), record["children"], ids)
        else:
            setattr(owner, name, _decode(record["value"], ids))


def _reset_cache(node):
    if node.bl_idname == compositor.DEVELOPER_TYPE:
        node.photo_uid = node.error = ""
        compositor.apply_developer_image(node, None, None)
        node["refresh_input"] = True
    elif node.bl_idname in film_node.FILM_TYPES:
        film_preview.discard_scan(node)
        film_node.invalidate(node)


def _prune(ids):
    # Only temporary IDs allocated by this transfer may be explicitly removed.
    pending = set(ids)
    while unused := {item for item in pending if not item.users and not item.library}:
        pending -= unused
        bpy.data.batch_remove(ids=unused)


def _snapshot_group(source, copies, *, cache_images=()):
    if source in copies:
        return copies[source]
    token = _copying_tree.set(True)
    try:
        tree = source.copy()
    finally:
        _copying_tree.reset(token)
    copies[source] = tree
    tree.use_fake_user = False
    # Every copied effect has an independent instance identity. Tree tags are
    # deliberately retained so the native editor recognises the effect type.
    for key in (compositor.PHOTO_TAG, compositor.DECODE_TAG):
        if key in tree:
            del tree[key]
    if "photo_editor_effect_uid" in tree:
        tree["photo_editor_effect_uid"] = uuid.uuid4().hex
    for node in tree.nodes:
        child = getattr(node, "node_tree", None)
        managed = node.bl_idname in (compositor.DEVELOPER_TYPE, *film_node.FILM_TYPES)
        if managed and not child:
            raise ValueError(f"{node.name}: its processing group is missing")
        if child and (managed or not child.library):
            cached = ()
            if node.bl_idname == compositor.DEVELOPER_TYPE:
                cached = ("As Shot RAW", "Developed RAW")
            elif node.bl_idname in film_node.FILM_TYPES:
                cached = ("Film Result",)
            node.node_tree = _snapshot_group(child, copies, cache_images=cached)
        if managed:
            node.instance_uid = uuid.uuid4().hex
        _reset_cache(node)
        # Auxiliary images, masks and Render Layers Scenes remain project
        # references. They must not be duplicated by libraries.load().
        refs = {}
        for prop in node.bl_rna.properties:
            key = prop.identifier
            if prop.type != "POINTER" or prop.is_readonly or key in {"node_tree", "lut_blob", "profile_blob"}:
                continue
            value = getattr(node, key)
            if isinstance(value, bpy.types.ID):
                if node.name not in cache_images or key != "image":
                    refs[key] = value.session_uid
                setattr(node, key, None)
        if refs:
            node[_EXTERNAL] = refs
    return tree


def copy(context, photo):
    global _clipboard
    scene = compositor.ensure_photo_scene(context, photo)
    reader = _reader_effect(scene)
    directory = TemporaryDirectory(prefix="photo-editor-adjustments-")
    allocated = set()
    actions_before = set(bpy.data.actions)
    effects = []
    active = scene.compositor_effects.active
    active_index = -1
    try:
        for effect in scene.compositor_effects:
            if effect == reader:
                continue
            copies = {}
            try:
                tree = _snapshot_group(effect.node_group, copies)
            finally:
                allocated.update(copies.values())
            tree.name = ".Photo Editor Adjustment"
            effects.append(EffectSnapshot(effect.name, tree.name,
                                          {name: getattr(effect, name) for name in _SETTINGS},
                                          _interface_state(effect.properties)))
            if effect == active:
                active_index = len(effects) - 1
        bpy.data.libraries.write(str(Path(directory.name) / "adjustments.blend"),
                                 {bpy.data.node_groups[e.group_name] for e in effects},
                                 path_remap="ABSOLUTE")
        snapshot = Clipboard(directory, photo.uid, effects, active_index)
    except Exception:
        directory.cleanup()
        raise
    finally:
        _prune(allocated | (set(bpy.data.actions) - actions_before))
    clear()
    _clipboard = snapshot


def _apply_effect(scene, snapshot, group, ids):
    effect = scene.compositor_effects.new(snapshot.name)
    try:
        effect.node_group = group
        _restore_interface(effect.properties, snapshot.properties, ids)
        for name, value in snapshot.settings.items():
            setattr(effect, name, value)
    except Exception:
        scene.compositor_effects.remove(effect)
        raise
    return effect


def _prepare(photo, scene, snapshot, ids, staging_scenes):
    reader = _reader_effect(scene)
    names = [effect.group_name for effect in snapshot.effects]
    with bpy.data.libraries.load(str(Path(snapshot.directory.name) / "adjustments.blend")) as (_, data):
        data.node_groups = names
    if any(group is None for group in data.node_groups):
        raise ValueError("The adjustment clipboard is incomplete")
    staging = bpy.data.scenes.new(".Photo Editor Paste")
    staging_scenes.append(staging)
    for record, root in zip(snapshot.effects, data.node_groups):
        root[compositor.PHOTO_TAG] = photo.uid
        if "photo_editor_effect_uid" in root:
            root["photo_editor_effect_uid"] = uuid.uuid4().hex
        for group in _groups(root):
            if group.library:
                continue
            for node in group.nodes:
                for key, uid in node.get(_EXTERNAL, {}).items():
                    if uid not in ids:
                        raise ValueError(f"{node.name}: a copied input is no longer in this project")
                    setattr(node, key, ids[uid])
                if _EXTERNAL in node:
                    del node[_EXTERNAL]
                if node.bl_idname in (compositor.DEVELOPER_TYPE, *film_node.FILM_TYPES):
                    node.instance_uid = uuid.uuid4().hex
                if node.bl_idname == compositor.DEVELOPER_TYPE:
                    node.photo_uid = photo.uid
                    node["recipe_dirty"] = True
        _apply_effect(staging, record, root, ids)
    return PastePlan(scene, reader, [e for e in scene.compositor_effects if e != reader], staging)


def paste(context, photos):
    snapshot = _clipboard
    if snapshot is None:
        return 0
    # A selection may be repeated by scripts. Each target must be replaced once.
    targets = list({p.uid: p for p in photos if p.uid != snapshot.source_uid}.values())
    if not targets:
        return 0
    scenes = Counter(p.scene for p in catalog.library(context).photos if p.scene)
    for photo in targets:
        if photo.scene:
            if scenes[photo.scene] > 1:
                raise ValueError(f"{photo.name}: its Scene is shared with another photo")
            _reader_effect(photo.scene)
    plans, staging_scenes, added, created_photos = [], [], [], []
    groups_before = set(bpy.data.node_groups)
    actions_before = set(bpy.data.actions)
    ids = {item.session_uid: item for item in bpy.data.all_ids}
    succeeded = False
    try:
        for photo in targets:
            if not photo.scene:
                created_photos.append(photo)
            scene = compositor.ensure_photo_scene(context, photo)
            plans.append((photo, _prepare(photo, scene, snapshot, ids, staging_scenes)))
        # Validate every target and every interface assignment in staging before
        # replacing any old effects. If allocation fails, the old stack survives.
        for photo, plan in plans:
            new_effects = []
            added.append((plan.scene, new_effects))
            for record, staged in zip(snapshot.effects, plan.staging.compositor_effects):
                new_effects.append(_apply_effect(plan.scene, record, staged.node_group, ids))
        for (photo, plan), (_, replacements) in zip(plans, added):
            # Keep old private groups alive for Blender's incremental undo.
            for effect in plan.old_effects:
                plan.scene.compositor_effects.remove(effect)
            for effect, record in zip(replacements, snapshot.effects):
                effect.name = record.name
            active = replacements[snapshot.active_index] if 0 <= snapshot.active_index < len(replacements) else plan.reader
            plan.scene.compositor_effects.active = active
            for key in ("edited_thumbnail_path", "edited_thumbnail_bytes"):
                if key in photo:
                    del photo[key]
            for effect in replacements:
                effect.node_group.update_tag()
                compositor._queue_source_sync(effect.node_group)
            plan.scene.update_tag()
        succeeded = True
    finally:
        if not succeeded:
            for scene, effects in added:
                for effect in effects:
                    scene.compositor_effects.remove(effect)
        for staging in staging_scenes:
            bpy.data.scenes.remove(staging)
        if not succeeded:
            for photo in created_photos:
                scene = photo.scene
                photo.scene = None
                if scene:
                    bpy.data.scenes.remove(scene)
        _prune((set(bpy.data.node_groups) - groups_before) | (set(bpy.data.actions) - actions_before))
    return len(plans)
