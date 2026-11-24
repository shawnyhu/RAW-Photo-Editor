"""Check that repeated native clipboard copies survive preview snapshots and project saves.

blender -b --factory-startup --python-exit-code 1 --python tests/node_clipboard.py
"""
from contextlib import ExitStack
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from blender_raw import catalog, compositor, film_node, saved_links


def copy_repeatedly(nodes):
    counts = [node.node_tree.users for node in nodes]
    assert all(counts)
    for _ in range(3):
        assert bpy.ops.node.clipboard_copy() == {"FINISHED"}
        assert [node.node_tree.users for node in nodes] == counts, "Copy changed private group users"


def branches(tree):
    for film in tree.nodes:
        if film.bl_idname == film_node.FILM_TYPE:
            dev = film.inputs["Image"].links[0].from_node
            reader = dev.inputs["Image"].links[0].from_node
            assert dev.bl_idname == compositor.DEVELOPER_TYPE and reader.bl_idname == compositor.READER_TYPE
            yield reader, dev, film


def run(folder):
    original, target = [bpy.data.node_groups.new(name, "CompositorNodeTree") for name in ("First", "Second")]
    original.use_fake_user = target.use_fake_user = True
    reader, dev, film = [original.nodes.new(kind) for kind in
                         (compositor.READER_TYPE, compositor.DEVELOPER_TYPE, film_node.FILM_TYPE)]
    reader.photo_uid, dev.exposure, film.film = "raw-id", 1.5, "kodak_portra_800"
    original.links.new(reader.outputs["Image"], dev.inputs["Image"])
    original.links.new(dev.outputs["Image"], film.inputs["Image"])
    for node in original.nodes:
        node.select = True
    original.nodes.active = film

    window = bpy.context.window
    area = max(window.screen.areas, key=lambda a: a.width * a.height)
    area.ui_type = "CompositorNodeTree"
    region = next(r for r in area.regions if r.type == "WINDOW")
    with bpy.context.temp_override(window=window, area=area, region=region):
        space = area.spaces.active
        space.pin = True
        space.path.start(original)
        copy_repeatedly((reader, dev, film))
        assert bpy.ops.node.duplicate() == {"FINISHED"}
        space.path.start(target)
        assert bpy.ops.node.clipboard_paste() == {"FINISHED"}
        copy_repeatedly(tuple(target.nodes))
        # Reproduce an already-open project damaged by the original copy hook.
        def legacy_copy(node, source):
            node.node_tree = source.node_tree.copy()
        with ExitStack() as hooks:
            for node in target.nodes:
                hooks.enter_context(patch.object(type(node), "copy", legacy_copy))
            assert bpy.ops.node.clipboard_copy() == {"FINISHED"}
        assert all(node.node_tree.users == 0 for node in target.nodes)

    sets = [*branches(original), *branches(target)]
    assert len(sets) == 3
    for originals in zip(*sets):
        assert len({node.node_tree.as_pointer() for node in originals}) == 3
    assert len({dev.instance_uid for _, dev, _ in sets}) == len({film.instance_uid for _, _, film in sets}) == 3
    assert all(reader.photo_uid == "raw-id" and dev.exposure == 1.5 and film.film == "kodak_portra_800"
               for reader, dev, film in sets)
    group_names = {node.node_tree.name for branch in sets for node in branch}
    viewer = original.nodes.new("CompositorNodeViewer")
    original.links.new(film.outputs["Image"], viewer.inputs["Image"]).is_muted = True
    expected_links = {tree.name: [saved_links.link_record(link) for link in tree.links]
                      for tree in (original, target)}
    snapshot = str(folder / "snapshot.blend")
    assert bpy.ops.wm.save_as_mainfile(filepath=snapshot, copy=True) == {"FINISHED"}
    assert all(not node.node_tree.use_extra_user for node in target.nodes)
    with bpy.data.libraries.load(snapshot) as (stored, _):
        assert group_names.issubset(stored.node_groups), "Worker snapshot omitted private groups"
    assert saved_links.read(snapshot, expected_links) == expected_links
    project = str(folder / "project.blend")
    assert bpy.ops.wm.save_as_mainfile(filepath=project) == {"FINISHED"}
    assert bpy.ops.wm.open_mainfile(filepath=project, load_ui=False) == {"FINISHED"}
    restored = [branch for name in ("First", "Second") for branch in branches(bpy.data.node_groups[name])]
    assert len(restored) == 3 and {n.node_tree.name for branch in restored for n in branch} == group_names
    print("PASS native copy, duplicate, paste, worker snapshot and project reload preserve all private groups")


catalog.register()
compositor.register()
try:
    with tempfile.TemporaryDirectory(prefix="photo-editor-clipboard-") as directory:
        run(Path(directory))
finally:
    compositor.unregister()
    catalog.unregister()
