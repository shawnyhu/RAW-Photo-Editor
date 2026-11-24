# SPDX-License-Identifier: GPL-3.0-or-later
"""Read native scene effects as one pipeline without maintaining a second graph."""
from pathlib import Path

import bpy


def effects(scene, mode='PREVIEW'):
    flag = {'PREVIEW': 'enable_for_preview', 'RENDER': 'enable_for_render'}.get(mode)
    return tuple(effect for effect in scene.compositor_effects
                 if effect.node_group and (flag is None or getattr(effect, flag)))


def photo_trees(photo):
    scene = getattr(photo, 'scene', None)
    return tuple(dict.fromkeys(e.node_group for e in effects(scene, 'ALL'))) if scene else ()


def node_instances(scene, node_type=None, mode='PREVIEW'):
    """Yield (effect, node, outer group nodes), preserving each executable instance."""
    def visit(effect, tree, parents, ancestors):
        if tree in ancestors:
            return
        for node in tree.nodes:
            if mode != 'ALL' and node.mute:
                continue
            if node_type is None or node.bl_idname == node_type:
                yield effect, node, parents
            if node.bl_idname == 'CompositorNodeGroup' and node.node_tree:
                yield from visit(effect, node.node_tree, (*parents, node), (*ancestors, tree))
    for effect in effects(scene, mode):
        yield from visit(effect, effect.node_group, (), ())


def instance_for_node(scene, node, mode='ALL'):
    found = [(effect, parents) for effect, candidate, parents in node_instances(scene, mode=mode)
             if candidate == node]
    if len(found) > 1:
        raise ValueError(f'Make the group containing {node.name} single-user before processing it')
    return found[0] if found else (None, ())


def effect_for_node(scene, node):
    return instance_for_node(scene, node)[0]


def output_socket(tree):
    end = next((n for n in tree.nodes if n.type == 'GROUP_OUTPUT' and n.is_active_output), None)
    return end.inputs[0] if end and end.inputs and end.inputs[0].type == 'RGBA' else None


def previous_output(scene, effect, mode='PREVIEW'):
    previous = None
    for candidate in effects(scene, mode):
        if candidate == effect:
            return (previous, output_socket(previous.node_group)) if previous else (None, None)
        previous = candidate
    return None, None


def effect_input(effect, identifier):
    """Return the value stored on this effect instance, not the group default."""
    inputs = getattr(effect.properties, 'inputs', None)
    item = getattr(inputs, identifier, None) if inputs else None
    return getattr(item, 'value', None)


def first_input(tree):
    return next((item.identifier for item in tree.interface.items_tree
                 if item.item_type == 'SOCKET' and item.in_out == 'INPUT'), None)


def _links(socket):
    return tuple(link for link in socket.links if link.is_valid and not link.is_muted)


def upstream(scene, inputs=None, *, effect=None, parents=(), mode='PREVIEW'):
    """Yield dependencies before consumers, crossing group and native effect inputs."""
    from . import compositor, film_node
    enabled = effects(scene, mode)
    if inputs is None:
        if not enabled:
            return
        effect = enabled[-1]
        socket = output_socket(effect.node_group)
        inputs = (socket,) if socket else ()
    seen = set()

    def follow(sockets, current, path):
        for socket in sockets:
            for link in _links(socket):
                yield from visit(link.from_socket, current, path)

    def visit(output, current, path):
        token = (current.as_pointer() if current else 0, output.as_pointer(),
                 tuple(group.as_pointer() for group in path))
        if token in seen:
            return
        seen.add(token)
        node = output.node
        if node.mute:
            yield from follow((link.from_socket for link in node.internal_links
                               if link.to_socket == output), current, path)
            return
        if node.bl_idname in film_node.FILM_TYPES and mode == 'PREVIEW' and not node.live_preview:
            cache = node.node_tree.nodes.get('Film Result') if node.node_tree else None
            if not cache or not cache.image:
                yield from follow((node.inputs['Image'],), current, path)
            return
        if node.bl_idname in {*film_node.FILM_TYPES, compositor.DEVELOPER_TYPE}:
            yield from follow((node.inputs['Image'],), current, path)
        elif node.bl_idname == compositor.READER_TYPE:
            pass
        elif node.type == 'GROUP_INPUT':
            if path:
                outer = next((s for s in path[-1].inputs if s.identifier == output.identifier), None)
                if outer:
                    yield from follow((outer,), current, path[:-1])
            elif current and output.identifier == first_input(current.node_group):
                previous, socket = previous_output(scene, current, mode)
                if socket:
                    yield from follow((socket,), previous, ())
        elif node.bl_idname == 'CompositorNodeGroup' and node.node_tree:
            end = next((n for n in node.node_tree.nodes
                        if n.type == 'GROUP_OUTPUT' and n.is_active_output), None)
            inner = next((s for s in end.inputs if s.identifier == output.identifier), None) if end else None
            if inner:
                yield from follow((inner,), current, (*path, node))
        elif node.bl_idname == 'CompositorNodeSwitch' and not _links(node.inputs['Switch']):
            yield from follow((node.inputs['On' if node.inputs['Switch'].default_value else 'Off'],), current, path)
        else:
            yield from follow((s for s in node.inputs if s.enabled), current, path)
        yield current, node, path

    yield from follow(inputs, effect, parents)


def source_socket(scene, socket, *, effect, parents=(), mode='PREVIEW', require_type=None):
    """Resolve the preceding processing output through transparent group boundaries."""
    seen = set()

    def from_input(value, current, path):
        if require_type and value.type != require_type:
            return None, None, ()
        links = _links(value)
        return resolve(links[0].from_socket, current, path) if links else (None, None, ())

    def resolve(output, current, path):
        if require_type and output.type != require_type:
            return None, None, ()
        token = (current.as_pointer() if current else 0, output.as_pointer(), tuple(n.as_pointer() for n in path))
        if token in seen:
            return None, None, ()
        seen.add(token)
        node = output.node
        if node.mute:
            inner = next((l.from_socket for l in node.internal_links if l.to_socket == output), None)
            return from_input(inner, current, path) if inner else (None, None, ())
        if node.type == 'REROUTE':
            return from_input(node.inputs[0], current, path)
        if node.type == 'GROUP_INPUT':
            if path:
                outer = next((s for s in path[-1].inputs if s.identifier == output.identifier), None)
                return from_input(outer, current, path[:-1]) if outer else (None, None, ())
            if current and output.identifier == first_input(current.node_group):
                previous, inner = previous_output(scene, current, mode)
                return from_input(inner, previous, ()) if inner else (None, None, ())
            return None, None, ()
        if node.bl_idname == 'CompositorNodeGroup' and node.node_tree:
            end = next((n for n in node.node_tree.nodes if n.type == 'GROUP_OUTPUT' and n.is_active_output), None)
            inner = next((s for s in end.inputs if s.identifier == output.identifier), None) if end else None
            return from_input(inner, current, (*path, node)) if inner else (None, None, ())
        return current, output, path

    return from_input(socket, effect, parents)


def immediate_image_source(scene, node, parents=(), mode='PREVIEW'):
    effect, found_parents = instance_for_node(scene, node, mode)
    socket = node.inputs.get('Image') if node.inputs else None
    if not effect or socket is None:
        return None
    _, output, _ = source_socket(scene, socket, effect=effect, parents=parents or found_parents, mode=mode)
    return output.node if output else None


def constant_input(scene, socket, *, effect, parents=(), mode='PREVIEW'):
    """Resolve a scalar constant, including native effect instance parameters."""
    links = _links(socket)
    if not links:
        return (True, socket.default_value) if hasattr(socket, 'default_value') else (False, None)
    output = links[0].from_socket
    node = output.node
    if node.type == 'REROUTE':
        return constant_input(scene, node.inputs[0], effect=effect, parents=parents, mode=mode)
    if node.type == 'GROUP_INPUT':
        if parents:
            outer = next((s for s in parents[-1].inputs if s.identifier == output.identifier), None)
            return constant_input(scene, outer, effect=effect, parents=parents[:-1], mode=mode) if outer else (False, None)
        if effect and output.identifier != first_input(effect.node_group):
            value = effect_input(effect, output.identifier)
            return (True, value) if value is not None else (False, None)
    if node.type == 'VALUE' and not node.id_data.animation_data:
        return True, output.default_value
    return False, None


def _freeze(value):
    if isinstance(value, bpy.types.ID):
        return value.bl_rna.identifier, value.name_full, value.session_uid
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        return tuple(_freeze(item) for item in value)
    except TypeError:
        return repr(value)


def stack_signature(scene, mode='PREVIEW'):
    """A read-only snapshot for edited thumbnails, including completed film pixels."""
    from . import film_preview
    state = [scene.session_uid, scene.frame_current_final, scene.render.resolution_x,
             scene.render.resolution_y, scene.render.resolution_percentage,
             bpy.data.colorspace.working_space, film_preview._values(scene.view_settings),
             film_preview._values(scene.display_settings)]
    for effect in effects(scene, mode):
        state.append((effect.name, effect.node_group.session_uid,
                      tuple((item.identifier, _freeze(effect_input(effect, item.identifier)))
                            for item in effect.node_group.interface.items_tree
                            if item.item_type == 'SOCKET' and item.in_out == 'INPUT')))
    seen = set()

    def visit(tree):
        if tree in seen:
            return
        seen.add(tree)
        state.append((tree.session_uid, tuple((link.from_node.name, link.from_socket.identifier,
                      link.to_node.name, link.to_socket.identifier, link.is_muted, link.is_valid) for link in tree.links)))
        for node in tree.nodes:
            skip = film_preview._DECORATION | {'layer', 'view'} if node.bl_idname == 'CompositorNodeImage' else film_preview._DECORATION
            state.append((node.name, node.bl_idname, film_preview._values(node, skip)))
            state.extend((s.identifier, _freeze(s.default_value)) for s in (*node.inputs, *node.outputs)
                         if hasattr(s, 'default_value'))
            image = getattr(node, 'image', None)
            if image:
                path = Path(bpy.path.abspath(image.filepath, library=image.library)) if image.filepath else None
                try:
                    stat = path.stat() if path else None
                except OSError:
                    stat = None
                state.append((image.session_uid, image.get('photo_editor_decode_cache'), image.is_dirty,
                              (stat.st_mtime_ns, stat.st_size) if stat else None))
            mapping = getattr(node, 'mapping', None)
            if mapping and hasattr(mapping, 'curves'):
                state.append(tuple(tuple((tuple(p.location), p.handle_type) for p in c.points) for c in mapping.curves))
            ramp = getattr(node, 'color_ramp', None)
            if ramp:
                state.append(tuple((p.position, tuple(p.color)) for p in ramp.elements))
            child = getattr(node, 'node_tree', None)
            if child:
                visit(child)
    for effect in effects(scene, mode):
        visit(effect.node_group)
    return tuple(state)
