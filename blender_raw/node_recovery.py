# SPDX-License-Identifier: GPL-3.0-or-later
"""Recover private compositor groups omitted by older versions during a clipboard copy."""
import bpy
from bpy.app.handlers import persistent

from . import compositor, film_node, saved_links


def _endpoints(record):
    return (record['from_node'], record['from_socket'], record['to_node'], record['to_socket'])


def _socket(sockets, identifier):
    return next((socket for socket in sockets if socket.identifier == identifier), None)


def restore_links(tree, records, repaired):
    # Rebuilding an interface can invalidate links even if its identifiers match.
    # Only recreate edges that touched a repaired owner. Keep other inputs intact.
    multi = {}
    added = set()
    for record in records:
        origin = tree.nodes.get(record['from_node'])
        target = tree.nodes.get(record['to_node'])
        if not origin or not target:
            continue
        output = _socket(origin.outputs, record['from_socket'])
        socket = _socket(target.inputs, record['to_socket'])
        if not output or not socket:
            continue
        link = next((edge for edge in socket.links if edge.from_socket == output), None)
        if origin.name in repaired or target.name in repaired:
            if not link and (socket.is_multi_input or not socket.is_linked):
                link = tree.links.new(output, socket)
                link.is_muted = record['muted']
                added.add(link)
        if link and socket.is_multi_input:
            multi.setdefault(socket, []).append((record['multi_input_sort_id'], link))

    for entries in multi.values():
        if not any(edge in added for _, edge in entries):
            continue
        # Put recovered links back in their saved slots while keeping the
        # relative order of surviving connections, including in an open project.
        stored = [edge for _, edge in sorted(entries, key=lambda x: x[0])]
        order = sorted(stored, key=lambda edge: edge.multi_input_sort_id)
        existing = iter(edge for edge in order if edge not in added)
        wanted = [edge if edge in added else next(existing) for edge in stored]
        for i, edge in enumerate(wanted):
            if edge != order[i]:
                j = order.index(edge, i + 1)
                edge.swap_multi_input_sort_id(order[i])
                order[i], order[j] = order[j], order[i]


def repair_groups(filepath=None):
    kinds = {compositor.READER_TYPE, compositor.DEVELOPER_TYPE, *film_node.FILM_TYPES}
    damaged = {tree: [n for n in tree.nodes if n.bl_idname in kinds and not n.node_tree]
               for tree in bpy.data.node_groups if tree.bl_idname == 'CompositorNodeTree'
               and not tree.library and tree.is_editable}
    damaged = {tree: nodes for tree, nodes in damaged.items() if nodes}
    if not damaged:
        return
    # When enabling the extension in an open file, Blender may still have the
    # serialized sockets. When already registered at file-open, it prunes them.
    missing_inputs = {tree: {n.name for n in nodes if not n.inputs and n.bl_idname != compositor.READER_TYPE}
                      for tree, nodes in damaged.items()}
    missing_outputs = {tree: {n.name for n in nodes if not n.outputs} for tree, nodes in damaged.items()}
    read_trees = [tree.name for tree in damaged if missing_inputs[tree] or missing_outputs[tree]]
    disk = {}
    if filepath and read_trees:
        try:
            disk = saved_links.read(filepath, read_trees)
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"RAW Photo Editor could not read saved compositor wiring: {exc}")

    for tree, nodes in damaged.items():
        records = [saved_links.link_record(link) for link in tree.links]
        if tree.name in disk:
            # Pruning can renumber surviving links on a multi-input socket.
            # For its original order, use the on-disk sort ID for surviving links too.
            current = {_endpoints(r): r for r in records}
            for r in disk[tree.name]:
                key = _endpoints(r)
                if key in current or r['from_node'] in missing_outputs[tree] or r['to_node'] in missing_inputs[tree]:
                    current[key] = r
            records = current.values()
        for node in nodes:
            if node.bl_idname in film_node.FILM_TYPES:
                film_node.restore_film_group(node)
            else:
                compositor.restore_raw_group(node)
        restore_links(tree, records, {node.name for node in nodes})
        compositor.finish_raw_recovery(tree, nodes)


@persistent
def _file_loaded(_):
    repair_groups(bpy.data.filepath)


def _on_enable():
    # File-open can mark a scene dirty before any edit. Consult disk only for
    # missing sockets; restore_links preserves already connected inputs.
    repair_groups(bpy.data.filepath)


def register():
    bpy.app.handlers.load_post.append(_file_loaded)
    if hasattr(bpy.data, 'node_groups'):
        _on_enable()
    else:
        bpy.app.timers.register(_on_enable, first_interval=0.0)


def unregister():
    if bpy.app.timers.is_registered(_on_enable):
        bpy.app.timers.unregister(_on_enable)
    bpy.app.handlers.load_post.remove(_file_loaded)
