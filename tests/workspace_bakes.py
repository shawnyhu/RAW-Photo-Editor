"""Blender 5.3: verify project bake extraction, failure recovery and reopening.

blender -b --factory-startup --python-exit-code 1 --python tests/workspace_bakes.py
"""
import hashlib
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import bpy

if "bl_ext.user_default.raw_photo_editor" in bpy.context.preferences.addons:
    from bl_ext.user_default.raw_photo_editor.workspace_bakes import unpack_project_bakes
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from blender_raw.workspace_bakes import unpack_project_bakes


def geometry_object(name):
    bpy.ops.mesh.primitive_cube_add()
    obj = bpy.context.object
    obj.name = name
    tree = bpy.data.node_groups.new(name, 'GeometryNodeTree')
    for direction in ('INPUT', 'OUTPUT'):
        tree.interface.new_socket(name='Geometry', in_out=direction, socket_type='NodeSocketGeometry')
    node = tree.nodes.new('GeometryNodeBake')
    node.bake_items.new('GEOMETRY', 'Geometry')
    tree.links.new(tree.nodes.new('NodeGroupInput').outputs[0], node.inputs[0])
    tree.links.new(node.outputs[0], tree.nodes.new('NodeGroupOutput').inputs[0])
    mod = obj.modifiers.new('Geometry', 'NODES')
    mod.node_group = tree
    bpy.context.view_layer.update()
    mod.bakes[0].bake_mode = 'STILL'
    return obj, mod.bakes[0]


def bake_now(obj, bake):
    with bpy.context.temp_override(active_object=obj, object=obj):
        assert bpy.ops.object.geometry_node_bake_single(
            session_uid=obj.session_uid, modifier_name='Geometry', bake_id=bake.bake_id) == {'FINISHED'}


def contents(folder):
    return {p.relative_to(folder): hashlib.sha256(p.read_bytes()).digest()
            for p in folder.rglob('*') if p.is_file()}


def settings(bake):
    return bake.use_custom_path, bake.directory, bake.bake_target


def unavailable():
    raise OSError("Workspace unavailable")


def check(root):
    obj, packed = geometry_object('Shot:Light')
    disk_obj, disk = geometry_object('Disk')
    _, empty = geometry_object('No bake')
    bpy.ops.wm.save_as_mainfile(filepath=str(root / 'before.blend'))
    disk.use_custom_path, disk.directory, disk.bake_target = True, '//original', 'DISK'
    bake_now(disk_obj, disk)
    bake_now(obj, packed)
    # Shared node group and bake ID, distinct objects with colliding sanitized names.
    sibling = obj.copy()
    sibling.name = 'Shot?Light'
    bpy.context.collection.objects.link(sibling)
    second = sibling.modifiers['Geometry'].bakes[0]
    assert second.bake_id == packed.bake_id
    assert bpy.path.clean_name(sibling.name) == bpy.path.clean_name(obj.name)
    empty.directory = '//unused'
    original = contents(root / 'original')
    assert set(p.suffix for p in original) == {'.json', '.blob'}
    recorded = [settings(b) for b in (packed, second, disk, empty)]

    try:
        unpack_project_bakes(unavailable)
    except OSError as error:
        assert str(error) == "Workspace unavailable"
    else:
        raise AssertionError("An unavailable workspace must leave the bake packed")
    assert [settings(b) for b in (packed, second, disk, empty)] == recorded

    workspace = root / 'project{samples}'
    assert unpack_project_bakes(lambda: workspace) == 2
    assert (settings(disk), settings(empty)) == tuple(recorded[2:])
    assert contents(root / 'original') == original
    assert packed.directory != second.directory
    for bake in (packed, second):
        assert bake.bake_target == 'DISK' and bake.directory.startswith('//')
        literal = bake.directory.replace('{{', '{').replace('}}', '}')
        target = Path(bpy.path.abspath(literal))
        assert target.is_relative_to(workspace) and contents(target) == original
    assert unpack_project_bakes(unavailable) == 0

    # Change the source mesh. After reopening the modifier must still produce the baked cube.
    mesh_x = obj.data.vertices[0].co.x
    obj.data.vertices[0].co.x = mesh_x + 5
    obj.data.update()
    project = root / 'after.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(project))
    bpy.ops.wm.open_mainfile(filepath=str(project), load_ui=False)
    assert unpack_project_bakes(unavailable) == 0
    bpy.context.view_layer.update()
    obj = bpy.data.objects['Shot:Light']
    assert obj.data.vertices[0].co.x == mesh_x + 5
    assert obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).data.vertices[0].co.x == mesh_x
    print("PASS packed nodes, colliding paths, existing disk bake, unused bake, "
          "workspace failure, idempotence and saved geometry", flush=True)


with TemporaryDirectory(prefix='photo-editor-workspace-bakes-') as folder:
    check(Path(folder))
