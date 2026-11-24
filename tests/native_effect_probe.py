"""Probe the installed Blender Scene Effects API."""
import bpy

scene = bpy.context.scene
effect = scene.compositor_effects.new("Probe")
group = bpy.data.node_groups.new("Probe", "CompositorNodeTree")
group.interface.new_socket(name="Image", in_out="INPUT", socket_type="NodeSocketColor")
group.interface.new_socket(name="Exposure", in_out="INPUT", socket_type="NodeSocketFloat")
group.interface.new_socket(name="Image", in_out="OUTPUT", socket_type="NodeSocketColor")
source = group.nodes.new("NodeGroupInput")
exposure = group.nodes.new("CompositorNodeExposure")
output = group.nodes.new("NodeGroupOutput")
group.links.new(source.outputs["Image"], exposure.inputs["Image"])
group.links.new(source.outputs["Exposure"], exposure.inputs["Exposure"])
group.links.new(exposure.outputs["Image"], output.inputs["Image"])
effect.node_group = group
print("EFFECT_RNA", [p.identifier for p in effect.bl_rna.properties])
print("PROPERTIES", effect.properties, dir(effect.properties))
print("INPUTS", dir(effect.properties.inputs))
for socket in group.interface.items_tree:
    if socket.item_type == "SOCKET" and socket.in_out == "INPUT":
        value = getattr(effect.properties.inputs, socket.identifier, None)
        print("INPUT", socket.name, socket.identifier, value, dir(value) if value else None)
print("OPS", [name for name in dir(bpy.ops.scene) if "compositor" in name])
