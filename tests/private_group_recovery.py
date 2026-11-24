"""Recover saved custom compositor nodes whose private groups were omitted by 0.15.2.

blender -b --factory-startup --python-exit-code 1 --python tests/private_group_recovery.py
"""
from pathlib import Path
from math import isclose
import sys
import tempfile

import bpy
import numpy as np
import OpenImageIO as oiio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from blender_raw import catalog, compositor, film_node, jobs, node_recovery


def wiring(tree):
    return {(link.from_node.name, link.from_socket.identifier,
             link.to_node.name, link.to_socket.identifier, link.is_muted) for link in tree.links}


def run(folder):
    tree = bpy.data.node_groups.new("Recovery • Custom Branches", "CompositorNodeTree")
    tree.use_fake_user = True
    tree.interface.new_socket(name="Result", in_out="OUTPUT", socket_type="NodeSocketColor")
    photo = bpy.context.scene.blender_raw.photos.add()
    photo.uid, photo.name, photo.tree = "test-photo", "Test photo", tree
    source = str(folder / "Test RAW pixels.exr")
    writer = oiio.ImageOutput.create(source)
    writer.open(source, oiio.ImageSpec(32, 24, 3, oiio.FLOAT))
    writer.write_image(np.full((24, 32, 3), 0.05, dtype=np.float32))
    writer.close()
    photo.image = bpy.data.images.load(source)
    tree[compositor.PHOTO_TAG] = photo.uid

    reader = tree.nodes.new(compositor.READER_TYPE)
    developer = tree.nodes.new(compositor.DEVELOPER_TYPE)
    catalog.set_camera_input(developer, 0)  # An existing project can have the original as-shot decoder.
    developer.auto_exposure, developer.exposure = "CENTER", 1.75
    developer.photo_uid = photo.uid
    native_branch = tree.nodes.new(compositor.DEVELOPER_TYPE)
    native_branch.name = "Keep the existing pipeline"
    catalog.set_camera_input(native_branch, 0)
    native_branch.auto_exposure, native_branch.photo_uid = "CENTER", photo.uid
    metered = compositor.metering.exposure_ev("CENTER", photo.image)
    assert abs(metered) > 0.1
    film = tree.nodes.new(film_node.FILM_TYPE)
    film.name = "After the native grade"
    film.film, film.paper = "kodak_portra_800", "kodak_2383"
    film.preset_name = "Portra 800 + 2383"
    film.grain, film.halation, film.live_preview = True, False, True
    loose = tree.nodes.new(film_node.FILM_TYPE)
    loose.name = "Deliberately disconnected"
    loose.film, loose.halation = "kodak_portra_400", True
    grade = tree.nodes.new("CompositorNodeExposure")
    junction = tree.nodes.new("NodeReroute")
    split = tree.nodes.new("ShaderNodeMix")
    split.data_type = "RGBA"
    output = tree.nodes.new("NodeGroupOutput")
    viewer = tree.nodes.new("CompositorNodeViewer")
    tree.links.new(reader.outputs["Image"], developer.inputs["Image"])
    tree.links.new(reader.outputs["Image"], native_branch.inputs["Image"])
    tree.links.new(developer.outputs["Image"], grade.inputs["Image"])
    tree.links.new(grade.outputs["Image"], junction.inputs[0])
    tree.links.new(junction.outputs[0], film.inputs["Image"])
    tree.links.new(film.outputs["Image"], split.inputs[6])
    tree.links.new(reader.outputs["Image"], split.inputs[7])
    tree.links.new(split.outputs[2], output.inputs["Result"])
    tree.links.new(film.outputs["Image"], viewer.inputs["Image"]).is_muted = True
    seen_links = wiring(tree)
    tree_name = tree.name
    uids = developer.instance_uid, film.instance_uid, loose.instance_uid
    damaged = folder / "missing-private-groups.blend"
    fixed = folder / "repaired.blend"

    # 0.15.2 clipboard_copy could decrement these counts while actual nodes
    # still referenced the groups. The writer then omitted them but kept the wires.
    groups = [node.node_tree for node in (reader, developer, film, loose)]
    group_names = {group.name for group in groups}
    for group in groups:
        group.user_clear()
    bpy.app.handlers.save_pre.remove(compositor._retain_legacy_groups)
    try:
        assert bpy.ops.wm.save_as_mainfile(filepath=str(damaged), copy=True) == {"FINISHED"}
    finally:
        bpy.app.handlers.save_pre.append(compositor._retain_legacy_groups)
    with bpy.data.libraries.load(str(damaged)) as (stored, _):
        assert tree_name in stored.node_groups and group_names.isdisjoint(stored.node_groups), stored.node_groups

    def verify():
        restored = bpy.data.node_groups[tree_name]
        reader = restored.nodes["Raw Reader"]
        developer = restored.nodes["Raw Developer"]
        film = restored.nodes["After the native grade"]
        loose = restored.nodes["Deliberately disconnected"]
        assert wiring(restored) == seen_links, (seen_links - wiring(restored), wiring(restored) - seen_links)
        assert all(node.node_tree and node.node_tree.users == 1 for node in (reader, developer, film, loose))
        assert (developer.instance_uid, film.instance_uid, loose.instance_uid) == uids
        assert reader.photo_uid == "test-photo" and reader.node_tree.nodes["Decoded Image"].image.name == "Test RAW pixels.exr"
        assert developer.exposure == 1.75 and developer.camera_input == "LEGACY" and developer.auto_exposure == "CENTER"
        assert isclose(developer.node_tree.nodes["Exposure"].inputs["Exposure"].default_value, 1.75 + metered, abs_tol=1e-5)
        preserved = restored.nodes["Keep the existing pipeline"]
        assert isclose(preserved.node_tree.nodes["Exposure"].inputs["Exposure"].default_value, metered, abs_tol=1e-5)
        assert film.film == "kodak_portra_800" and film.paper == "kodak_2383" and film.grain and not film.halation
        assert film.preset_name == "Portra 800 + 2383" and film.live_preview
        assert loose.film == "kodak_portra_400" and loose.halation and not loose.live_preview
        assert not loose.inputs["Image"].is_linked and not loose.outputs["Image"].is_linked
        return {node.name: node.node_tree.name for node in (reader, developer, film, loose)}

    node_recovery.register()
    jobs.register()
    try:
        assert bpy.ops.wm.open_mainfile(filepath=str(damaged), load_ui=False, use_scripts=False) == {"FINISHED"}
        names = verify()
        assert bpy.ops.wm.save_as_mainfile(filepath=str(fixed)) == {"FINISHED"}
        assert bpy.ops.wm.open_mainfile(filepath=str(fixed), load_ui=False, use_scripts=False) == {"FINISHED"}
        assert verify() == names
    finally:
        jobs.unregister()
        node_recovery.unregister()


catalog.register()
compositor.register()
try:
    with tempfile.TemporaryDirectory(prefix="photo-editor-recovery-") as directory:
        run(Path(directory))
    print("PASS legacy private groups, original custom links, recipes and second reopen")
finally:
    compositor.unregister()
    catalog.unregister()
