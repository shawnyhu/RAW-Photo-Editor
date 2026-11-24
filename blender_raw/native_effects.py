# SPDX-License-Identifier: GPL-3.0-or-later
"""Photo processing stages in Blender's native Scene Effects stack.

The stack and its input properties belong to Blender. Backend controls keep
their existing node RNA and are drawn inside each native effect's panel.
"""
from pathlib import Path
import time
import uuid

import bpy
from bpy.app.handlers import persistent
from bpy.props import StringProperty

from . import catalog, compositor, crop_node, film_node


KIND_TAG = "photo_editor_native_effect"
UID_TAG = "photo_editor_effect_uid"
PRIMARY_NODE_TAG = "photo_editor_effect_node"
CUSTOM_TYPE = "CUSTOM"
_isolating = False
_registered = False
_structure_signatures = {}
_group_owners = {}
_ownership_signature = None
_ownership_dirty = True
_ui_original_draw = None
_ui_ready_regions = set()
_ui_pending_regions = set()
_ui_reset_scene = None
_ui_reset_created = 0.0
_ui_reset_used = False
_ui_cleanup = False
_UI_RESET_TAG = 'photo_editor_ui_refresh'


def _ui_region_key(context):
    if context.screen and context.region:
        return context.screen.as_pointer(), context.region.as_pointer()
    return None


def _ui_panel_ready(context):
    """Only dereference child custom_data after native instancing rebuilt it.

    Blender 5.3 adds Python children to existing native panels without their
    custom_data pointer. Its RNA getter does not check for null, so a Python
    try/except around self.custom_data cannot make that path safe.
    """
    return not _ui_cleanup and _ui_region_key(context) in _ui_ready_regions


def _ui_reset_scene_get():
    try:
        if _ui_reset_scene and bpy.data.scenes.get(_ui_reset_scene.name) == _ui_reset_scene:
            return _ui_reset_scene
    except ReferenceError:
        pass
    return None


def _ui_reset_scene_create():
    global _ui_reset_scene, _ui_reset_created, _ui_reset_used
    if _ui_reset_scene_get() is None:
        _ui_reset_scene = bpy.data.scenes.new('.Photo Editor UI Refresh')
        _ui_reset_scene[_UI_RESET_TAG] = True
        _ui_reset_created = time.monotonic()
        _ui_reset_used = False
    return _ui_reset_scene


def _ui_reset_scene_remove():
    global _ui_reset_scene, _ui_reset_used
    scene = _ui_reset_scene_get()
    _ui_reset_scene = None
    _ui_reset_used = False
    if scene is not None:
        bpy.data.scenes.remove(scene)


def _ui_before_draw(self, context):
    """Refresh native instances before Blender's unchanged native draw."""
    global _ui_reset_used
    if not _registered and not _ui_cleanup:
        return
    key = _ui_region_key(context)
    if _ui_cleanup or key not in _ui_ready_regions:
        reset_scene = _ui_reset_scene_get()
        if reset_scene is not None:
            # An empty native list frees its old panels with a valid context,
            # including animation handlers. Drawing the real list afterwards
            # recreates children with the correct effect custom_data pointer.
            with context.temp_override(scene=reset_scene):
                self.layout.template_scene_compositor_effects()
            _ui_reset_used = True
            _ui_pending_regions.discard(key)
            if not _ui_cleanup:
                _ui_ready_regions.add(key)
        elif key is not None:
            # Data-block creation is forbidden during draw. The maintenance
            # timer supplies a temporary empty scene for the next layout pass.
            _ui_pending_regions.add(key)


def _draw_compositor_button(self, context):
    """Place the workspace toggle below Add Scene Effect, above native cards."""
    if not context.scene or not context.scene.get(compositor.STUDIO_TAG):
        return
    from . import gallery
    viewer, nodes = gallery.viewer_column(context.screen)
    row = self.layout.row()
    row.enabled = viewer is not None
    row.operator('blender_raw.toggle_compositor', text='Compositor', icon='NODETREE',
                 depress=nodes is not None)


def _ui_visible_regions():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != 'PROPERTIES' or area.spaces.active.context != 'COMPOSITOR':
                continue
            region = next((item for item in area.regions if item.type == 'WINDOW'), None)
            if region:
                yield window, area, region, (window.screen.as_pointer(), region.as_pointer())


def _ui_service_refresh():
    if bpy.app.background:
        return
    visible = list(_ui_visible_regions())
    _ui_pending_regions.intersection_update(item[3] for item in visible)
    if _ui_reset_scene_get() is not None:
        # Never retain a helper scene in the user's project. Regions opened
        # later request another brief refresh through the same timer.
        if _ui_reset_used or time.monotonic() - _ui_reset_created > .5:
            _ui_reset_scene_remove()
    if _ui_pending_regions:
        _ui_reset_scene_create()
        for _window, area, _region, key in visible:
            if key in _ui_pending_regions:
                area.tag_redraw()


@persistent
def _ui_clear_transients(*_args):
    global _ui_reset_scene
    _ui_reset_scene = None
    for scene in list(bpy.data.scenes):
        if scene.get(_UI_RESET_TAG):
            bpy.data.scenes.remove(scene)


@persistent
def _ui_file_loaded(*_args):
    _ui_clear_transients()
    _ui_ready_regions.clear()
    _ui_pending_regions.clear()


def _ui_install():
    global _ui_original_draw, _ui_cleanup
    if bpy.app.background:
        return
    _ui_cleanup = False
    _ui_ready_regions.clear()
    _ui_pending_regions.clear()
    _ui_original_draw = bpy.types.SCENE_PT_compositor_effects.draw
    # Extension enable runs inside RestrictBlend: bpy.data is unavailable here.
    # The first draw queues its region; the maintenance timer creates the
    # temporary scene after registration, then safely rebuilds native panels.
    bpy.types.SCENE_PT_compositor_effects.prepend(_ui_before_draw)
    bpy.types.SCENE_PT_compositor_effects.append(_draw_compositor_button)
    bpy.app.handlers.load_post.append(_ui_file_loaded)
    for name in ('save_pre', 'undo_post', 'redo_post'):
        getattr(bpy.app.handlers, name).append(_ui_clear_transients)


def _ui_prepare_unregister():
    global _ui_cleanup
    if bpy.app.background or _ui_original_draw is None:
        return
    _ui_cleanup = True
    _ui_ready_regions.clear()
    _ui_reset_scene_create()
    panel_type = bpy.types.SCENE_PT_compositor_effects
    current_draw = panel_type.draw
    # Another add-on may have wrapped/replaced draw after us. Borrow it only
    # for this synchronous cleanup and restore exactly what we found.
    panel_type.draw = _ui_before_draw
    try:
        seen_windows = set()
        for window, area, region, _key in list(_ui_visible_regions()):
            if window.as_pointer() in seen_windows:
                continue
            seen_windows.add(window.as_pointer())
            for visible_window, visible_area, _region, _key in _ui_visible_regions():
                if visible_window == window:
                    visible_area.tag_redraw()
            with bpy.context.temp_override(window=window, area=area, region=region):
                # DRAW_WIN only draws cached regions. SWAP runs wm_draw_update
                # and therefore executes the layout/template cleanup above.
                bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
    finally:
        panel_type.draw = current_draw


def _ui_uninstall():
    global _ui_original_draw, _ui_cleanup
    if not bpy.app.background:
        panel_type = bpy.types.SCENE_PT_compositor_effects
        panel_type.remove(_ui_before_draw)
        panel_type.remove(_draw_compositor_button)
        callbacks = getattr(panel_type.draw, '_draw_funcs', ())
        if len(callbacks) == 1 and callbacks[0] is _ui_original_draw:
            panel_type.draw = _ui_original_draw
        for handlers, callback in ((bpy.app.handlers.load_post, _ui_file_loaded),
                                   (bpy.app.handlers.save_pre, _ui_clear_transients),
                                   (bpy.app.handlers.undo_post, _ui_clear_transients),
                                   (bpy.app.handlers.redo_post, _ui_clear_transients)):
            if callback in handlers:
                handlers.remove(callback)
    _ui_clear_transients()
    _ui_original_draw = None
    _ui_cleanup = False
    _ui_ready_regions.clear()
    _ui_pending_regions.clear()


def _processing_types():
    return {compositor.READER_TYPE, compositor.DEVELOPER_TYPE, crop_node.TYPE, *film_node.FILM_TYPES}


def _has_processing(tree, memo=None, ancestors=()):
    if memo is None:
        memo = {}
    if tree in memo:
        return memo[tree]
    if tree in ancestors:
        return False
    result = any(node.bl_idname in _processing_types() or
                 (getattr(node, 'node_tree', None) and
                  _has_processing(node.node_tree, memo, (*ancestors, tree)))
                 for node in tree.nodes)
    memo[tree] = result
    return result


def effect_node(effect):
    """Return the processing node owned by a photo effect, if it has one."""
    tree = effect.node_group if effect else None
    if tree and tree.get(KIND_TAG) in _processing_types():
        return next((node for node in tree.nodes if node.get(PRIMARY_NODE_TAG)), None)
    return None


def iter_effects(scene):
    """Iterate native effect order, including user-created effects and assets."""
    return iter(scene.compositor_effects)


def owner_effect(node):
    """Find the persistent native effect owning a processing node."""
    if not node or not node.id_data.get(KIND_TAG):
        return None
    preferred = _group_owners.get(node.id_data.session_uid)
    found = []
    for scene in bpy.data.scenes:
        for effect in scene.compositor_effects:
            if effect.node_group == node.id_data:
                if preferred and preferred[:2] == (scene.session_uid, effect.as_pointer()):
                    return scene, effect
                found.append((scene, effect))
    return found[0] if found else None


def _remember_owner(scene, effect):
    def visit(tree, path=(), ancestors=()):
        if tree in ancestors:
            return
        key = (scene.session_uid, effect.as_pointer(), path)
        if not path:
            _group_owners[tree.session_uid] = key
        else:
            _group_owners.setdefault(tree.session_uid, key)
        for node in tree.nodes:
            if child := getattr(node, 'node_tree', None):
                visit(child, (*path, node.name), (*ancestors, tree))
    if effect.node_group:
        visit(effect.node_group)


def input_property(effect, name):
    """Resolve a native input using its interface identifier, never its index."""
    tree = effect.node_group if effect else None
    if tree:
        socket = next((item for item in tree.interface.items_tree
                       if item.item_type == 'SOCKET' and item.in_out == 'INPUT'
                       and item.name == name), None)
        if socket:
            return getattr(effect.properties.inputs, socket.identifier, None)
    return None


def get_input_value(effect, name, default=None):
    prop = input_property(effect, name)
    return getattr(prop, 'value', default) if prop else default


def set_input_value(effect, name, value):
    prop = input_property(effect, name)
    if prop is None or not hasattr(prop, 'value'):
        raise KeyError(f"{effect.name}: no native input named {name}")
    prop.value = value


def _new_group(label, kind, photo):
    tree = bpy.data.node_groups.new(label, 'CompositorNodeTree')
    tree[KIND_TAG] = kind
    tree[UID_TAG] = uuid.uuid4().hex
    if photo:
        tree[compositor.PHOTO_TAG] = photo.uid
    # A Reader is an independent image source. Giving it a phantom scene-image
    # input can make render preflight request a camera in an empty photo scene.
    if kind != compositor.READER_TYPE:
        tree.interface.new_socket(name='Image', in_out='INPUT', socket_type='NodeSocketColor')
    tree.interface.new_socket(name='Image', in_out='OUTPUT', socket_type='NodeSocketColor')
    source = tree.nodes.new('NodeGroupInput')
    source.name = 'Effect Input'
    source.location = (-300, 80)
    output = tree.nodes.new('NodeGroupOutput')
    output.name = 'Effect Output'
    output.location = (300, 80)
    return tree, source, output


def _clone_group(source, copies=None, processing=None):
    """Copy private graphs outside Node.copy's Main lock, preserving all links."""
    from . import adjustments
    if copies is None:
        copies = {}
    if processing is None:
        processing = {}
    if source in copies:
        return copies[source]
    token = adjustments._copying_tree.set(True)
    try:
        tree = source.copy()
    finally:
        adjustments._copying_tree.reset(token)
    copies[source] = tree
    tree.use_fake_user = False
    if tree.get(KIND_TAG):
        tree[UID_TAG] = uuid.uuid4().hex
    for node in tree.nodes:
        child = getattr(node, 'node_tree', None)
        if child and (node.bl_idname in _processing_types() or _has_processing(child, processing)):
            node.node_tree = _clone_group(child, copies, processing)
        if node.bl_idname in {compositor.DEVELOPER_TYPE, *film_node.FILM_TYPES}:
            node.instance_uid = uuid.uuid4().hex
        if node.bl_idname == compositor.DEVELOPER_TYPE:
            node.error = ''
        elif node.bl_idname in film_node.FILM_TYPES:
            node.status = ''
    return tree


def _create_effect(scene, kind, *, photo=None, group=None):
    """Append an independent photo stage or editable Custom Compositor effect."""
    names = {compositor.READER_TYPE: 'Raw Reader', compositor.DEVELOPER_TYPE: 'Raw Developer',
             film_node.FILM_TYPE: 'Spektrafilm Lite', film_node.OFX_TYPE: 'Spektrafilm OFX',
             crop_node.TYPE: 'Crop & Perspective', CUSTOM_TYPE: 'Custom Compositor'}
    if kind not in names:
        raise ValueError(f'Unsupported photo effect: {kind}')
    if kind == CUSTOM_TYPE and group:
        if group.bl_idname != 'CompositorNodeTree':
            raise ValueError('Scene Effects require a compositor node group')
        tree = _clone_group(group)
        tree[KIND_TAG] = CUSTOM_TYPE
        tree[UID_TAG] = uuid.uuid4().hex
        if photo:
            tree[compositor.PHOTO_TAG] = photo.uid
    else:
        tree, source, output = _new_group(names[kind], kind, photo)
        if kind == CUSTOM_TYPE:
            tree.links.new(source.outputs['Image'], output.inputs['Image'])
        else:
            node = tree.nodes.new(kind)
            node.name = names[kind]
            node[PRIMARY_NODE_TAG] = True
            node.location = (0, 80)
            if kind != compositor.READER_TYPE:
                tree.links.new(source.outputs['Image'], node.inputs['Image'])
            elif photo:
                compositor._bind_node(node, photo)
            if kind == compositor.DEVELOPER_TYPE:
                if 'Exposure' not in node.inputs:
                    raise RuntimeError('Raw Developer requires its native Exposure input')
                socket = tree.interface.new_socket(name='Exposure', in_out='INPUT', socket_type='NodeSocketFloat')
                socket.default_value = 0.0
                socket.min_value, socket.max_value = -12.0, 12.0
                socket.description = 'Exposure compensation in stops'
                tree.links.new(source.outputs['Exposure'], node.inputs['Exposure'])
                if photo:
                    node.photo_uid = photo.uid
                    compositor._copy_photo_recipe(node, photo)
            tree.links.new(node.outputs['Image'], output.inputs['Image'])
            tree.nodes.active = node
    effect = scene.compositor_effects.new(names[kind])
    effect.node_group = tree
    effect.show_node_group_selector = kind == CUSTOM_TYPE
    scene.compositor_effects.active = effect
    _remember_owner(scene, effect)
    return effect


def create_effect(scene, kind, *, photo=None, group=None):
    """Append a stage; remove incomplete allocations if its backend cannot load."""
    groups_before = set(bpy.data.node_groups)
    effects_before = {effect.as_pointer() for effect in scene.compositor_effects}
    try:
        return _create_effect(scene, kind, photo=photo, group=group)
    except Exception:
        for effect in list(scene.compositor_effects):
            if effect.as_pointer() not in effects_before:
                scene.compositor_effects.remove(effect)
        pending = set(bpy.data.node_groups) - groups_before
        while unused := {tree for tree in pending if not tree.users and not tree.library}:
            pending -= unused
            bpy.data.batch_remove(ids=unused)
        raise


def _copy_input_values(source, destination):
    if not source.node_group:
        return
    for socket in source.node_group.interface.items_tree:
        if socket.item_type != 'SOCKET' or socket.in_out != 'INPUT':
            continue
        before = getattr(source.properties.inputs, socket.identifier, None)
        after = getattr(destination.properties.inputs, socket.identifier, None)
        if before is not None and after is not None and hasattr(before, 'value') and hasattr(after, 'value'):
            after.value = before.value


def duplicate_effect(scene, source):
    """Duplicate a stage atomically for plugin operators and batch operations."""
    tree = _clone_group(source.node_group) if source.node_group else None
    target = scene.compositor_effects.new(source.name)
    target.node_group = tree
    target.enable_for_preview = source.enable_for_preview
    target.enable_for_render = source.enable_for_render
    target.show_expanded = source.show_expanded
    target.show_node_group_selector = source.show_node_group_selector
    _copy_input_values(source, target)
    _remember_owner(scene, target)
    return target


def ensure_independent_effects(scene=None):
    """Isolate managed groups shared by Blender's native duplicate/copy actions.

    Native duplication shares node groups. Every path containing a RAW/Film
    processor must be independent, including untagged user-created effects and
    nested groups shared by different effect roots. Pure assets stay shared.
    """
    global _isolating, _ownership_signature, _ownership_dirty
    if _isolating:
        return 0
    _isolating = True
    changed = 0
    try:
        references = {}
        roots = []
        private = set()
        processing = {}

        def gather(owner, effect, tree, path=(), ancestors=()):
            if tree in ancestors:
                return
            key = (owner.session_uid, effect.as_pointer(), path)
            references.setdefault(tree.session_uid, []).append(key)
            for node in tree.nodes:
                child = getattr(node, 'node_tree', None)
                if child:
                    if node.bl_idname in _processing_types():
                        private.add(child.session_uid)
                    gather(owner, effect, child, (*path, node.name), (*ancestors, tree))

        for owner in bpy.data.scenes:
            for effect in owner.compositor_effects:
                tree = effect.node_group
                if tree:
                    roots.append((owner, effect))
                    gather(owner, effect, tree)

        # Preserve known editing owners rather than whichever scene sorts first.
        # New references to an existing subtree must not displace its original.
        preferred = {}
        for group_uid, keys in references.items():
            saved = _group_owners.get(group_uid)
            keep = saved if saved in keys else keys[0]
            if scene is not None:
                keep = next((key for key in keys if key[0] != scene.session_uid), keep)
            preferred[group_uid] = keep

        live_groups = set()

        def isolate(owner, effect, tree, path=(), parent=None, ancestors=()):
            nonlocal changed
            if tree in ancestors:
                return
            key = (owner.session_uid, effect.as_pointer(), path)
            mutable = bool(tree.get(KIND_TAG) or tree.session_uid in private or _has_processing(tree, processing))
            keep = preferred.setdefault(tree.session_uid, key)
            if mutable and keep != key and not owner.library and (scene is None or owner == scene):
                tree = _clone_group(tree, processing=processing)
                if parent is None:
                    effect.node_group = tree
                else:
                    parent.node_tree = tree
                preferred[tree.session_uid] = key
                changed += 1
            live_groups.add(tree.session_uid)
            if mutable:
                _group_owners[tree.session_uid] = key
            for node in tree.nodes:
                child = getattr(node, 'node_tree', None)
                if child:
                    if node.bl_idname in _processing_types():
                        private.add(child.session_uid)
                    isolate(owner, effect, child, (*path, node.name), node, (*ancestors, tree))

        for owner, effect in roots:
            isolate(owner, effect, effect.node_group)
        for key in _group_owners.keys() - live_groups:
            del _group_owners[key]
        _ownership_signature = _root_signature()
        _ownership_dirty = False
        return changed
    finally:
        _isolating = False


def _active(context):
    effect = context.scene.compositor_effects.active if context.scene else None
    return effect, effect_node(effect)


def _active_photo(context):
    return catalog.lookup_photo(None, context.scene.get(compositor.PHOTO_TAG, ''))


class BR_OT_add_native_effect(bpy.types.Operator):
    bl_idname = 'blender_raw.add_native_effect'
    bl_label = 'Add Photo Effect'
    bl_options = {'REGISTER', 'UNDO'}
    kind: StringProperty(options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return context.scene is not None and context.scene.is_editable

    def execute(self, context):
        photo = _active_photo(context)
        if self.kind == compositor.READER_TYPE and not photo:
            self.report({'ERROR'}, 'Select a photo in Photo Gallery before adding Raw Reader')
            return {'CANCELLED'}
        try:
            create_effect(context.scene, self.kind, photo=photo)
        except (RuntimeError, ValueError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class BR_OT_duplicate_native_effect(bpy.types.Operator):
    bl_idname = 'blender_raw.duplicate_native_effect'
    bl_label = 'Duplicate Photo Effect'
    bl_description = 'Create an independent copy of this effect and its processing settings'
    bl_options = {'REGISTER', 'UNDO'}
    effect_name: StringProperty(options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return bool(context.scene and context.scene.is_editable)

    def execute(self, context):
        effect = (context.scene.compositor_effects.get(self.effect_name) if self.effect_name
                  else context.scene.compositor_effects.active)
        if not effect or not effect.node_group:
            return {'CANCELLED'}
        ensure_independent_effects()
        duplicate_effect(context.scene, effect)
        return {'FINISHED'}


class BR_OT_reset_native_effect(bpy.types.Operator):
    bl_idname = 'blender_raw.reset_native_effect'
    bl_label = 'Reset Photo Effect'
    bl_description = 'Reset this effect to its default settings'
    bl_options = {'REGISTER', 'UNDO'}
    effect_name: StringProperty(options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return bool(context.scene and context.scene.is_editable)

    def execute(self, context):
        ensure_independent_effects()
        effect = (context.scene.compositor_effects.get(self.effect_name) if self.effect_name
                  else context.scene.compositor_effects.active)
        node = effect_node(effect)
        if not node or node.bl_idname == compositor.READER_TYPE:
            return {'CANCELLED'}
        if node.bl_idname == compositor.DEVELOPER_TYPE:
            compositor.reset_developer(node, context)
            set_input_value(effect, 'Exposure', 0.0)
        elif node.bl_idname == crop_node.TYPE:
            crop_node.reset(node, context)
        else:
            film_node.reset_recipe(node)
        return {'FINISHED'}


def _draw_metadata(layout, photo):
    info = photo.get('capture_info', {})
    name = catalog.original_filename(photo)
    for label, value in (
            ('Captured', info.get('captured') or photo.capture_date),
            ('File', ' · '.join(filter(None, (Path(name).suffix[1:].upper(), info.get('size'))))),
            ('Rating', f'{photo.rating} / 5' + (' · Flagged' if photo.flagged else '')),
            ('Camera', info.get('camera')), ('Lens', info.get('lens')),
            ('ISO', info.get('iso')), ('Shutter', info.get('shutter')), ('Aperture', info.get('aperture'))):
        row = layout.split(factor=.32)
        row.label(text=label)
        row.label(text=str(value or '—'))


class BR_PT_native_effect_controls(bpy.types.Panel):
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = 'compositor'
    bl_parent_id = 'SCENE_COMPOSITOR_EFFECT_PT'
    bl_label = 'Settings'

    @classmethod
    def poll(cls, context):
        return bool(context.scene and (context.scene.get(compositor.STUDIO_TAG)
                    or any(effect_node(effect) for effect in context.scene.compositor_effects)))

    def draw_header_preset(self, context):
        if not _ui_panel_ready(context):
            return
        effect = self.custom_data
        node = effect_node(effect)
        if node and node.bl_idname != compositor.READER_TYPE:
            self.layout.operator(BR_OT_reset_native_effect.bl_idname, text='',
                                 icon='LOOP_BACK', emboss=False).effect_name = effect.name

    def draw(self, context):
        if not _ui_panel_ready(context):
            return
        # Each native panel instance passes its own effect to its children.
        # The active effect may be a completely different card.
        effect = self.custom_data
        if not effect:
            return
        node = effect_node(effect)
        if not node:
            return
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.context_pointer_set('active_node', node)
        # A duplicate can be drawn before Blender emits the depsgraph update.
        # Do not expose mutable node controls while two effects still share it.
        owners = sum(effect.node_group == other.node_group for scene in bpy.data.scenes
                     for other in scene.compositor_effects)
        if owners > 1:
            layout.label(text='Preparing independent effect controls…', icon='INFO')
            return
        if node.bl_idname == compositor.READER_TYPE:
            node.draw_buttons(context, layout)
            photo = catalog.lookup_photo(None, node.photo_uid)
            if photo:
                _draw_metadata(layout, photo)
        elif node.bl_idname == compositor.DEVELOPER_TYPE:
            node.draw_controls(layout, show_source=True)
        else:
            node.draw_buttons(context, layout)
        state = compositor._preview_status(node)
        if state:
            from . import node_status
            layout.separator()
            layout.label(text=state[1], icon_value=node_status.icon_id(state[0]))


def _add_menu(self, _context):
    self.layout.separator()
    for kind, label in ((compositor.READER_TYPE, 'Raw Reader'),
                        (compositor.DEVELOPER_TYPE, 'Raw Developer'),
                        (film_node.FILM_TYPE, 'Spektrafilm Lite'),
                        (film_node.OFX_TYPE, 'Spektrafilm OFX'), (crop_node.TYPE, 'Crop & Perspective'),
                        (CUSTOM_TYPE, 'Custom Compositor')):
        op = self.layout.operator(BR_OT_add_native_effect.bl_idname, text=label, icon='NODE_COMPOSITING')
        op.kind = kind


@persistent
def _isolate_handler(*_args):
    global _ownership_dirty
    if _isolating or bpy.app.is_job_running('RENDER'):
        return
    graph = _args[-1] if _args else None
    if hasattr(graph, 'updates'):
        if any(isinstance(update.id, bpy.types.NodeTree) for update in graph.updates):
            _ownership_dirty = True
    else:
        _ownership_dirty = True  # File load, Undo/Redo or explicit save.
    if _ownership_dirty or _ownership_signature != _root_signature():
        ensure_independent_effects()


def _root_signature():
    return tuple((scene.session_uid, effect.as_pointer(), effect.node_group.session_uid if effect.node_group else None)
                 for scene in bpy.data.scenes for effect in scene.compositor_effects)


def _isolate_timer():
    if not _registered:
        return None
    if not bpy.app.is_job_running('RENDER'):
        _ui_service_refresh()
        if _ownership_dirty or _ownership_signature != _root_signature():
            ensure_independent_effects()
        sync_stack_changes()
    return .25


def sync_stack_changes():
    """Queue source reconciliation once after native stack structural edits.

    Native effect order/enable flags do not necessarily call Node.update.
    Reading only scene/effect metadata keeps the unchanged timer path cheap.
    """
    if bpy.app.background:
        return
    live = set()
    for scene in bpy.data.scenes:
        if not scene.get(compositor.STUDIO_TAG):
            continue
        key = scene.session_uid
        live.add(key)
        signature = tuple((effect.node_group.session_uid if effect.node_group else None,
                           effect.enable_for_preview, effect.enable_for_render)
                          for effect in scene.compositor_effects)
        if _structure_signatures.get(key) == signature:
            continue
        _structure_signatures[key] = signature
        photo = catalog.lookup_photo(scene, scene.get(compositor.PHOTO_TAG, ''))
        if photo:
            compositor._set_dimensions(scene, photo)
        queued = set()
        def queue_tree(tree):
            if tree.session_uid in queued:
                return
            queued.add(tree.session_uid)
            compositor._queue_source_sync(tree)
            for node in tree.nodes:
                if child := getattr(node, 'node_tree', None):
                    queue_tree(child)
        for effect in scene.compositor_effects:
            if effect.node_group:
                queue_tree(effect.node_group)
    for key in _structure_signatures.keys() - live:
        del _structure_signatures[key]


_classes = (BR_OT_add_native_effect, BR_OT_duplicate_native_effect, BR_OT_reset_native_effect,
            BR_PT_native_effect_controls)
_handlers = ('depsgraph_update_post', 'undo_post', 'redo_post', 'load_post', 'save_pre')


def register():
    global _registered
    _ui_install()
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.SCENE_MT_add_compositor_effect.append(_add_menu)
    for name in _handlers:
        getattr(bpy.app.handlers, name).append(_isolate_handler)
    _registered = True
    if not bpy.app.background:
        bpy.app.timers.register(_isolate_timer, first_interval=.1, persistent=True)


def unregister():
    global _registered
    _registered = False
    _ui_prepare_unregister()
    _structure_signatures.clear()
    _group_owners.clear()
    if bpy.app.timers.is_registered(_isolate_timer):
        bpy.app.timers.unregister(_isolate_timer)
    for name in _handlers:
        handlers = getattr(bpy.app.handlers, name)
        if _isolate_handler in handlers:
            handlers.remove(_isolate_handler)
    bpy.types.SCENE_MT_add_compositor_effect.remove(_add_menu)
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
    _ui_uninstall()
