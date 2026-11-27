# SPDX-License-Identifier: GPL-3.0-or-later
"""Compositor nodes exposed by the Adjust scene-effect menu."""
from . import levels_node


NODES = {
    levels_node.TYPE: 'Auto Level',
    'CompositorNodeBrightContrast': 'Brightness/Contrast',
    'CompositorNodeColorBalance': 'Color Balance',
    'CompositorNodeColorCorrection': 'Color Correction',
    'CompositorNodeExposure': 'Exposure',
    'ShaderNodeGamma': 'Gamma',
    'CompositorNodeHueCorrect': 'Hue Correct',
    'CompositorNodeHueSat': 'Hue/Saturation/Value',
    'CompositorNodeCurveRGB': 'RGB Curves',
    'CompositorNodeTonemap': 'Tonemap',
}


def image_socket(sockets, kind):
    """Get the image socket from a stage's inputs or outputs."""
    return sockets['Color' if kind == 'ShaderNodeGamma' else 'Image']


def draw(layout, node):
    if node.bl_idname == levels_node.TYPE:
        from . import levels_ui
        import bpy
        levels_ui.draw(layout, node, bpy.context)
        return
    # Blender 5.3's generic template draws every Tonemap input, including
    # the ones the chosen algorithm never evaluates.
    if node.bl_idname == 'CompositorNodeTonemap' and not node.inputs['Type'].is_linked:
        mode = node.inputs['Type']
        layout.prop(mode, 'default_value', text='Type')
        names = (('Key', 'Balance', 'Gamma') if mode.default_value == 'Rh Simple' else
                 ('Intensity', 'Contrast', 'Light Adaptation', 'Chromatic Adaptation'))
        for name in names:
            socket = node.inputs[name]
            if not socket.is_linked:
                layout.prop(socket, 'default_value', text=socket.name)
        return
    layout.template_node_inputs(node)
