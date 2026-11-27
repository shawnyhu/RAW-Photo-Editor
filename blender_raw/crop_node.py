# SPDX-License-Identifier: GPL-3.0-or-later
"""A single compositor node with a scene-linear projective resampling stage."""
import math
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, StringProperty
from . import crop_geometry as geom

TYPE = 'BlenderRawCropPerspective'
DIRECTIONS = (
    ('VERTICAL', 'Vertical', 'Straighten two upright lines, such as building edges'),
    ('HORIZONTAL', 'Horizontal', 'Level two lines, such as the top and bottom of a facade'),
    ('BOTH', 'Both', 'Straighten both pairs of lines together'),
)
_busy = False


def make_tree():
    tree = bpy.data.node_groups.new('.Photo Crop and Perspective', 'CompositorNodeTree')
    for direction in ('INPUT', 'OUTPUT'):
        tree.interface.new_socket(name='Image', in_out=direction, socket_type='NodeSocketColor')
    def node(kind, name, x, y):
        n = tree.nodes.new(kind)
        n.name, n.label, n.location = name, name, (x, y)
        return n
    link = tree.links.new
    source = node('NodeGroupInput','Source',0,300).outputs['Image']
    out = node('NodeGroupOutput','Output',1940,300)
    info = node('CompositorNodeImageInfo','Source Size',180,10)
    link(source, info.inputs['Image'])
    split = node('ShaderNodeSeparateXYZ','Dimensions',360,10)
    link(info.outputs['Dimensions'], split.inputs[0])
    shape = node('ShaderNodeCombineXYZ','Output Size',860,10)
    for i, label in enumerate(('Width','Height')):
        mul = node('ShaderNodeMath',label,540,-i*170)
        mul.operation = 'MULTIPLY'
        mul.inputs[1].default_value = 1.
        link(split.outputs[i], mul.inputs[0])
        rounded = node('ShaderNodeMath','Round '+label,700,-i*170)
        rounded.operation = 'ROUND'
        link(mul.outputs[0],rounded.inputs[0])
        link(rounded.outputs[0],shape.inputs[i])
    canvas = node('CompositorNodeBlankImage','Crop Canvas',1030,-10)
    link(shape.outputs[0],canvas.inputs['Size'])
    coordinates = node('CompositorNodeImageCoordinates','Pixel Centers',0,-490)
    link(canvas.outputs[0],coordinates.inputs[0])
    xy = node('ShaderNodeSeparateXYZ','Crop UV',180,-490)
    link(coordinates.outputs['Normalized'], xy.inputs[0])
    xyz = node('ShaderNodeCombineXYZ','Homogeneous UV',360,-490)
    for i in range(2):
        link(xy.outputs[i],xyz.inputs[i])
    xyz.inputs[2].default_value = 1.
    dots = []
    for i, name in enumerate(('Source U','Source V','Divisor')):
        dot = node('ShaderNodeVectorMath',name,550,-400-i*180)
        dot.operation = 'DOT_PRODUCT'
        dot.inputs[1].default_value = tuple(float(i==j) for j in range(3))
        link(xyz.outputs[0],dot.inputs[0])
        dots.append(dot.outputs['Value'])
    uv = node('ShaderNodeCombineXYZ','Sample UV',1060,-440)
    uv.inputs[2].default_value = 1.
    for i in range(2):
        divide = node('ShaderNodeMath','Project '+('U','V')[i],830,-400-i*180)
        divide.operation = 'DIVIDE'
        link(dots[i],divide.inputs[0])
        link(dots[2],divide.inputs[1])
        link(divide.outputs[0],uv.inputs[i])
    sample = node('CompositorNodeMapUV','Sample Source',1470,300)
    # The geometry stays inside the source boundary. At a tangency, bilinear
    # filtering still needs the edge texel for the outer half-pixel footprint.
    for axis in ('Extension X','Extension Y'):
        socket = sample.inputs[axis]
        for identifier in ('Extend','EXTEND'):
            try:
                socket.default_value = identifier
                break
            except TypeError:
                continue
        else:
            raise RuntimeError('Could not enable the Map UV edge sampler')
    link(source,sample.inputs['Image'])
    link(uv.outputs[0],sample.inputs['UV'])
    link(source,out.inputs['Image'])
    return tree


def parameters(node):
    return dict(preset=node.preset, flip=node.flip, free=node.free_ratio, size=node.crop_size,
                position=tuple(node.center), angle=node.angle, mode=node.mode, guides=tuple(node.guides),
                direction=node.direction, bounded_guides=node.bounded_guides)


def solve(p, aspect):
    active = p['mode'] != 'OFF'
    return geom.frame(aspect, geom.ratio_for(p['preset'], aspect, p['free'], p['flip']),
                      p['guides'] if active else geom.UNIT, p['angle'], p['size'], p['position'],
                      axis=p.get('direction', 'BOTH') if active else 'BOTH',
                      legacy_bounds=active and not p.get('bounded_guides', False))


def sync(node, aspect):
    f = solve(parameters(node), aspect)
    tree = node.node_tree
    if not tree:
        return f
    for key, value in zip(('Width','Height'), f.fractions):
        socket = tree.nodes[key].inputs[1]
        if abs(socket.default_value-value) > 1e-7:
            socket.default_value = value
    for name, row in zip(('Source U','Source V','Divisor'), f.inverse):
        socket = tree.nodes[name].inputs[1]
        if not np.allclose(socket.default_value, row, atol=1e-7, rtol=1e-7):
            socket.default_value = row
    identity = np.allclose(f.inverse/f.inverse[2,2], np.eye(3), atol=1e-7)
    src = tree.nodes['Source' if identity else 'Sample Source'].outputs[0]
    dst = tree.nodes['Output'].inputs[0]
    if not dst.links or dst.links[0].from_socket != src:
        tree.links.new(src,dst)
    return f


def terminal_dimensions(scene, width, height, mode):
    from . import adjust_effects, compositor, film_node, scene_pipeline
    effects = scene_pipeline.effects(scene, mode)
    if not effects:
        return width, height
    effect = effects[-1]
    out = scene_pipeline.output_socket(effect.node_group)
    if out is None:
        return width, height
    # Follow the image through color adjustments. Keep the effect and group path
    # so two instances of the same custom group resolve their own inputs.
    same_size = {*adjust_effects.NODES, *film_node.FILM_TYPES, compositor.DEVELOPER_TYPE,
                 'CompositorNodeGamma'}
    crops = []
    visited = set()
    parents = ()
    while out is not None:
        effect, socket, parents = scene_pipeline.source_socket(scene, out, effect=effect, parents=parents, mode=mode)
        if socket is None:
            break
        node = socket.node
        kind = node.bl_idname
        key = (effect.as_pointer(), node.as_pointer(), tuple(group.as_pointer() for group in parents))
        if key in visited:
            break
        visited.add(key)
        if kind == TYPE:
            crops.append(node)
        elif kind not in same_size:
            if crops and kind == 'CompositorNodeImage' and node.image and all(node.image.size):
                width, height = node.image.size
            break
        out = adjust_effects.image_socket(node.inputs, kind) if kind in adjust_effects.NODES else node.inputs.get('Image')
    for crop in reversed(crops):
        fitted = sync(crop, width / height)
        width, height = (max(1, math.floor(v + .5)) for v in np.array((width, height)) * fitted.fractions)
    return width, height


def edited(node, context):
    if _busy or not node.node_tree:
        return
    from . import catalog, compositor, native_effects
    owner = native_effects.owner_effect(node)
    scene = owner[0] if owner else getattr(context,'scene',None)
    sync(node, node.reference_aspect)
    if scene and scene.get(compositor.PHOTO_TAG):
        photo = catalog.lookup_photo(scene,scene[compositor.PHOTO_TAG])
        if photo:
            compositor._set_dimensions(scene, photo)


def apply(node, context, p, aspect):
    global _busy
    solve(p, aspect)  # Check the complete transaction before changing any RNA.
    _busy = True
    try:
        node.reference_aspect = aspect
        node.preset, node.flip, node.free_ratio = p['preset'], p['flip'], p['free']
        node.crop_size, node.center, node.angle = p['size'], p['position'], p['angle']
        node.guides, node.mode, node.direction = p['guides'], p['mode'], p.get('direction', 'BOTH')
        node.bounded_guides = p.get('bounded_guides', False)
    finally:
        _busy = False
    edited(node, context)


def reset(node, context):
    apply(node,context,dict(preset='FREE', flip=False, free=0., size=1., position=(.5,.5),
                           angle=0., mode='OFF', guides=geom.GUIDES, bounded_guides=True),node.reference_aspect)
    node.edit_direction = 'BOTH'


@contextmanager
def reference(scene, node):
    """Snapshot only the input. The output of an already corrected node is unsuitable for guides."""
    from . import scene_pipeline, film_bake
    source = scene_pipeline.immediate_image_source(scene,node)
    if source and source.bl_idname=='CompositorNodeImage' and source.image and all(source.image.size):
        yield source.image
        return
    effect, parents = scene_pipeline.instance_for_node(scene,node,'PREVIEW')
    if not effect:
        raise ValueError('Enable the effect in the current scene to edit its image')
    with TemporaryDirectory(prefix='photo-crop-') as folder:
        path = film_bake._capture(scene,effect.node_group,node,parents,Path(folder),mode='PREVIEW')
        image = bpy.data.images.load(str(path),check_existing=False)
        try:
            image.use_view_as_render = True
            yield image
        finally:
            bpy.data.images.remove(image)


class BR_NODE_crop(bpy.types.CompositorNodeCustomGroup):
    bl_idname = TYPE
    bl_label = 'Crop & Perspective'
    bl_icon = 'IMAGE_DATA'
    bl_width_default = 245
    preset: EnumProperty(name='Aspect', items=[
        ('FREE','Free','Drag a crop corner to change aspect'),('ORIGINAL','Original','Lock to the source aspect'),
        ('SQUARE','1:1',''),('FIVE_FOUR','5:4',''),('FOUR_THREE','4:3',''),
        ('THREE_TWO','3:2',''),('WIDE','16:9',''),('CINEMA','2.39:1','')], update=edited)
    flip: BoolProperty(name='Swap Width / Height',update=edited)
    free_ratio: FloatProperty(default=0., min=0., options={'HIDDEN'},update=edited)
    crop_size: FloatProperty(name='Crop Size', default=1., min=.01,max=1.,subtype='FACTOR',update=edited)
    center: FloatVectorProperty(name='Crop Center',size=2,default=(.5,.5),min=0.,max=1.,update=edited)
    angle: FloatProperty(name='Rotation',subtype='ANGLE',unit='ROTATION',min=-math.pi,max=math.pi,update=edited)
    mode: EnumProperty(name='Perspective',items=[('OFF','Off',''),('MANUAL','Manual','Align guides by hand'),
                                                ('AUTO','Auto','Use detected guides')],update=edited)
    direction: EnumProperty(name='Applied Direction', items=DIRECTIONS, default='BOTH',
                            options={'HIDDEN'}, update=edited)
    edit_direction: EnumProperty(name='Guide Direction', items=DIRECTIONS, default='BOTH',
                                 description='Choose the lines to use for Edit Guides or Auto Detect')
    # Missing on saved nodes from beta.1 and earlier. Keep their exact crop until guides are reapplied.
    bounded_guides: BoolProperty(default=False, options={'HIDDEN'})
    guides: FloatVectorProperty(size=8,default=geom.GUIDES,options={'HIDDEN'},update=edited)
    reference_aspect: FloatProperty(default=1.5,min=.0001,options={'HIDDEN'})

    @classmethod
    def poll(cls, tree):
        return tree.bl_idname=='CompositorNodeTree'

    def init(self, context):
        self.bounded_guides = True
        self.node_tree = make_tree()
        scene = getattr(context,'scene',None)
        if scene:
            self.reference_aspect = scene.render.resolution_x / scene.render.resolution_y

    def copy(self, source):
        from . import adjustments
        if not adjustments.borrowing_groups() and not self.id_data.is_runtime_data and source.node_tree:
            self.node_tree = source.node_tree.copy()

    def draw_buttons(self,context,layout):
        def button(row, action, title):
            op = row.operator('blender_raw.edit_crop',text=title)
            op.tree_name,op.node_name,op.action = self.id_data.name,self.name,action
        row = layout.row(align=True)
        row.prop(self,'preset',text='')
        row.prop(self,'flip',text='',icon='ARROW_LEFTRIGHT')
        layout.prop(self,'angle')
        row = layout.row(align=True)
        row.prop(self,'crop_size',slider=True)
        button(row,'FIT','Fit')
        button(layout,'CROP','Edit Crop Frame')
        layout.separator()
        status = self.mode.title()
        if self.mode != 'OFF':
            status += ' · ' + self.direction.title()
        layout.label(text='Perspective · ' + status)
        row = layout.row(align=True)
        row.use_property_split = False
        row.prop(self, 'edit_direction', expand=True)
        row = layout.row(align=True)
        button(row,'GUIDES','Edit Guides')
        button(row,'AUTO','Auto Detect')
        if self.mode!='OFF':
            button(layout,'OFF','Disable Perspective')
    draw_buttons_ext = draw_buttons
