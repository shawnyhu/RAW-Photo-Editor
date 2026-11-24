"""Recover missing private OFX/Lite groups without losing saved graph wiring."""
from pathlib import Path
import sys
import tempfile

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from blender_raw import catalog, compositor, film_node, node_recovery, ofx_node


def links(tree):
    return {(edge.from_node.name, edge.from_socket.identifier, edge.to_node.name,
             edge.to_socket.identifier, edge.is_muted) for edge in tree.links}


catalog.register()
compositor.register()
node_recovery.register()
try:
    tree = bpy.data.node_groups.new('OFX and Lite recovery', 'CompositorNodeTree')
    tree.use_fake_user = True
    tree.interface.new_socket(name='Image', in_out='OUTPUT', socket_type='NodeSocketColor')
    source = tree.nodes.new('CompositorNodeImage')
    ofx = tree.nodes.new(film_node.OFX_TYPE)
    lite = tree.nodes.new(film_node.FILM_TYPE)
    sink = tree.nodes.new('NodeGroupOutput')
    setattr(ofx, ofx_node.parameter_property('filmExposureEv'), 1.25)
    lite.film = 'kodak_portra_800'
    tree.links.new(source.outputs['Image'], ofx.inputs['Image'])
    tree.links.new(ofx.outputs['Image'], lite.inputs['Image'])
    tree.links.new(lite.outputs['Image'], sink.inputs['Image'])
    expected = links(tree)
    name, ofx_name, lite_name = tree.name, ofx.name, lite.name
    with tempfile.TemporaryDirectory(prefix='ofx-recovery-') as folder:
        path = str(Path(folder) / 'missing-groups.blend')
        for node in (ofx, lite):
            node.node_tree.user_clear()
        bpy.app.handlers.save_pre.remove(compositor._retain_legacy_groups)
        try:
            bpy.ops.wm.save_as_mainfile(filepath=path, copy=True)
        finally:
            bpy.app.handlers.save_pre.append(compositor._retain_legacy_groups)
        bpy.ops.wm.open_mainfile(filepath=path, load_ui=False, use_scripts=False)
        restored = bpy.data.node_groups[name]
        assert links(restored) == expected
        for node_name in (ofx_name, lite_name):
            assert restored.nodes[node_name].node_tree.nodes.get('Film Result')
        assert getattr(restored.nodes[ofx_name], ofx_node.parameter_property('filmExposureEv')) == 1.25
        assert restored.nodes[lite_name].film == 'kodak_portra_800'
        bpy.ops.wm.save_as_mainfile(filepath=str(Path(folder) / 'repaired.blend'))
    print('PASS missing OFX/Lite private groups, saved wire recovery and independent recipes')
finally:
    node_recovery.unregister()
    compositor.unregister()
    catalog.unregister()
