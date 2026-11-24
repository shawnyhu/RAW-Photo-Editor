# SPDX-License-Identifier: GPL-3.0-or-later
"""Installed Blender smoke test with actual pixels and a trapezoidal line chart."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import gzip
import time
import bpy
import numpy as np
import OpenImageIO as oiio
from bl_ext.user_default.raw_photo_editor import (
    catalog, compositor, crop_detect, crop_geometry as g, crop_node, jobs, native_effects, scene_pipeline)


def geometry():
    for aspect in (.7,1,1.5):
        for angle in (-1.57,-.71,0,.34,1.57):
            for ratio in (.5625,1,1.5):
                f = g.frame(aspect,ratio,g.UNIT,angle,1.,(.5,.5))
                uv = g.project(f.inverse,g.UNIT)
                assert uv.min()>-1e-6 and uv.max()<1+1e-6
                assert np.min(np.minimum(abs(uv),abs(uv-1))) < 1e-5
                assert abs(f.extent[0]/f.extent[1]-ratio)<1e-8
    points = np.array(((.10,.15),(.90,.20),(.77,.85),(.26,.82)))
    shape = (192,256)
    y,x = np.indices(shape)
    gray = np.full(shape,.1)
    for a,b in zip(points,np.roll(points,-1,axis=0)):
        a,b = a*np.array(shape[::-1]),b*np.array(shape[::-1])
        e = b-a
        t = ((x-a[0])*e[0]+(y-a[1])*e[1])/(e@e)
        distance = abs((x-a[0])*e[1]-(y-a[1])*e[0])/np.linalg.norm(e)
        gray += np.where((t>=0)&(t<=1),np.exp(-distance**2/2),0)
    detected = np.reshape(crop_detect.detect(gray),(4,2))
    np.testing.assert_allclose(detected,points,atol=.035)
    try:
        crop_detect.detect(np.zeros(shape))
    except ValueError:
        pass
    else:
        raise AssertionError('Blank image produced guides')


def pixels(folder):
    scene = bpy.context.scene
    settings = scene.render.image_settings
    settings.file_format,settings.color_depth,settings.color_mode='OPEN_EXR','32','RGBA'
    scene.render.compositor_device='CPU'
    scene.render.compositor_precision='FULL'
    scene.render.resolution_percentage=100
    width,height=180,120
    y,x=np.indices((height,width))
    arr=np.ones((height,width,4),np.float32)
    arr[:,:,0],arr[:,:,1],arr[:,:,2]=(x+.5)/width,1-(y+.5)/height,1.3
    src=folder/'reference.exr'
    with closing(oiio.ImageOutput.create(str(src))) as writer:
        assert writer.open(str(src),oiio.ImageSpec(width,height,4,oiio.FLOAT))
        assert writer.write_image(arr)
    img=bpy.data.images.load(str(src))
    img.colorspace_settings.name='Linear Rec.709'
    photo=catalog.library(bpy.context).photos.add()
    photo.uid,photo.name,photo.image='test-crop','Projective chart',img
    tree=bpy.data.node_groups.new('Input chart','CompositorNodeTree')
    tree.interface.new_socket(name='Image',in_out='OUTPUT',socket_type='NodeSocketColor')
    source=tree.nodes.new('CompositorNodeImage')
    source.image=img
    tree.links.new(source.outputs[0],tree.nodes.new('NodeGroupOutput').inputs[0])
    scene.compositor_effects.new('Source').node_group=tree
    effect=native_effects.create_effect(scene,crop_node.TYPE)
    node=native_effects.effect_node(effect)
    assert node and crop_node.TYPE==node.bl_idname
    with crop_node.reference(scene,node) as reference:
        assert reference==img
    geom=crop_node.parameters(node)
    for name, changes in [('identity',{}),('portrait',dict(preset='WIDE',flip=True)),
                          ('turn',dict(preset='THREE_TWO',angle=.24)),
                          ('guides',dict(mode='MANUAL',guides=(.15,.14,.85,.18,.72,.86,.23,.79))),
                          ('reframe',dict(preset='FREE',free=.85,size=.65,position=(.34,.67)))]:
        p={**geom,**changes}
        crop_node.apply(node,bpy.context,p,width/height)
        out=folder/(name+'.exr')
        compositor.export_photo(bpy.context,photo,out,scene)
        with closing(oiio.ImageInput.open(str(out))) as reader:
            actual=reader.read_image(format=oiio.FLOAT)
        f=crop_node.solve(p,width/height)
        w,h=np.maximum(1,np.floor(np.array((width,height))*f.fractions+.5).astype(int))
        assert actual.shape==(h,w,4),(name,actual.shape,h,w)
        yy,xx=np.indices((h,w))
        uv=np.column_stack((((xx+.5)/w).ravel(),(1-(yy+.5)/h).ravel()))
        expected=g.project(f.inverse,uv).reshape(h,w,2)
        np.testing.assert_allclose(actual[2:-2,2:-2,:2],expected[2:-2,2:-2],atol=.0012)
        assert np.min(actual[:,:,3])>.98,(name,actual[:,:,3].min())
        np.testing.assert_allclose(actual[2:-2,2:-2,2],1.3,atol=.001)
    effect_copy=native_effects.duplicate_effect(scene,effect)
    duplicate=native_effects.effect_node(effect_copy)
    assert duplicate.node_tree!=node.node_tree
    duplicate.angle=.5
    assert node.angle!=duplicate.angle
    blend=str(folder/'crop.blend')
    bpy.ops.wm.save_as_mainfile(filepath=blend)
    bpy.ops.wm.open_mainfile(filepath=blend)
    saved=[native_effects.effect_node(e) for e in bpy.context.scene.compositor_effects]
    assert sum(bool(n and n.bl_idname==crop_node.TYPE and n.node_tree) for n in saved)==2


def gallery(folder):
    fixture=Path(__file__).resolve().parent/'fixtures'/'highlight-chart.dng.gz'
    path=folder/'Crop chart.dng'
    path.write_bytes(gzip.decompress(fixture.read_bytes()))
    photo,_=catalog.add_photo(bpy.context,path)
    scene=compositor.ensure_photo_scene(bpy.context,photo)
    dev=next(n for _,n,_ in scene_pipeline.node_instances(scene,compositor.DEVELOPER_TYPE))
    dev.camera_input,dev.look='AUTO','NONE'
    dev.auto_exposure,dev.white_balance='AS_SHOT','CAMERA'
    effect=native_effects.create_effect(scene,crop_node.TYPE,photo=photo)
    node=native_effects.effect_node(effect)
    node.preset='SQUARE'
    catalog.ensure_full(bpy.context,photo,scratch=str(folder))
    catalog.ensure_developer_full(bpy.context,photo,dev,scratch=str(folder))
    with crop_node.reference(scene,node) as reference:
        source_dims=tuple(reference.size)
        assert source_dims[0]!=source_dims[1] and min(source_dims)>10,source_dims
    settings=scene.render.image_settings
    settings.file_format,settings.color_mode='PNG','RGBA'
    # The camera RAW is decoded in a separate installed Blender process.
    out=folder/'gallery'
    with bpy.context.temp_override(scene=scene):
        jobs.start_export(bpy.context,[photo],out)
    deadline=time.monotonic()+120
    while jobs.export_running():
        assert time.monotonic()<deadline,jobs.export_status()
        jobs._poll_export()
        time.sleep(.06)
    assert not jobs.export_details(),jobs.export_details()
    result=next(out.glob('*.png'))
    with closing(oiio.ImageInput.open(str(result))) as reader:
        w,h=reader.spec().width,reader.spec().height
        assert w==h==min(source_dims),(w,h,source_dims)
        arr=reader.read_image(format=oiio.FLOAT)
        assert arr[:,:,0].max()>.05 and arr[:,:,3].min()>.99
    print('PASS RAW Gallery background export',w,h,flush=True)


geometry()
with TemporaryDirectory() as directory:
    pixels(Path(directory))
    gallery(Path(directory))
print('PASS crop geometry, automatic guides, installed scene effects, EXR pixel coordinates, HDR, alpha, duplicate and reopen')
