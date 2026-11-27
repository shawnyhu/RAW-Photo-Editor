# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional, private real-photo regression for Auto Level's lifted-black bug.

Set PHOTO_LEVELS_RAW to a local RAW; this test never ships or modifies that file.
Run an installed extension with -- --installed. A source run needs rawpy on
Blender's Python path, or --reuse-input pointing to a previously generated EXR.
--record-only records the former beta.7 formula without asserting the new graph.
--output selects a LOCAL evidence folder; do not publish photographic derivatives.

The recreated Portra 800 / Vision 2383 + Grain + Camera Diffusion recipe follows
the screenshot's visible selections. It is not the user's complete saved recipe.
"""
import argparse
from contextlib import closing
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import bpy
import numpy as np
import OpenImageIO as oiio


def write(path, pixels, *, display=False):
    values = np.array(pixels, dtype=np.float32, copy=True)
    if display:
        rgb = np.clip(values[..., :3], 0., 1.)
        values[..., :3] = np.where(rgb <= .0031308, rgb * 12.92,
                                   1.055 * np.power(rgb, 1 / 2.4) - .055)
    spec = oiio.ImageSpec(values.shape[1], values.shape[0], values.shape[2],
                          oiio.UINT8 if display else oiio.FLOAT)
    spec.attribute('oiio:ColorSpace', 'sRGB' if display else 'Linear Rec.709')
    with closing(oiio.ImageOutput.create(str(path))) as writer:
        assert writer.open(str(path), spec)
        assert writer.write_image(values)


def read(path):
    with closing(oiio.ImageInput.open(str(path))) as reader:
        assert reader is not None, path
        return reader.read_image(format=oiio.FLOAT)


def decode(path, edge):
    import rawpy
    with rawpy.imread(str(path)) as raw:
        pixels = raw.postprocess(use_camera_wb=True, no_auto_bright=True,
                                 gamma=(1., 1.), output_bps=16,
                                 output_color=rawpy.ColorSpace.sRGB,
                                 half_size=True).astype(np.float32) / 65535.
    height, width = pixels.shape[:2]
    scale = min(1., edge / max(height, width))
    if scale < 1:
        target = oiio.ImageBuf(oiio.ImageSpec(max(1, round(width * scale)),
                                             max(1, round(height * scale)), 3, oiio.FLOAT))
        assert oiio.ImageBufAlgo.resize(target, oiio.ImageBuf(pixels))
        pixels = target.get_pixels(format=oiio.FLOAT)
    return np.ascontiguousarray(pixels, dtype=np.float32)


def film_input(addon, raw, folder, edge):
    node_module = importlib.import_module(addon.__name__ + '.film_node')
    engine = importlib.import_module(addon.__name__ + '.spektrafilm_engine')
    tree = bpy.data.node_groups.new('Real Photo Film Recipe', 'CompositorNodeTree')
    node = tree.nodes.new(node_module.FILM_TYPE)
    node.film = 'kodak_portra_800'
    node.paper = 'kodak_2383'
    node.grain = True
    node.cameraDiffusionEnabled = True
    node.halation = False
    node.scannerEnabled = False
    recipe = node_module.recipe(node)
    recipe['frame'] = 1.
    pixels = decode(raw, edge)
    rgba = np.ones((*pixels.shape[:2], 4), dtype=np.float32)
    rgba[..., :3] = pixels
    result = engine.render_rgba(rgba, **recipe)
    assert np.isfinite(result).all()
    metadata = dict(source_name=raw.name,
                    source_sha256=hashlib.sha256(raw.read_bytes()).hexdigest(),
                    interpretation='Approximate screenshot recipe; not the complete user recipe',
                    decoder='rawpy, camera WB, linear sRGB, no auto brightness, half decode then resize',
                    recipe=recipe, shape=list(result.shape))
    (folder / 'input-metadata.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    write(folder / 'input-film.exr', result)
    return result


def native_result(addon, pixels, folder):
    effects = importlib.import_module(addon.__name__ + '.native_effects')
    levels = importlib.import_module(addon.__name__ + '.levels_node')
    scene = bpy.data.scenes.new('Real Photo Auto Level Regression')
    scene.render.engine = 'BLENDER_WORKBENCH'
    scene.display.render_aa = 'OFF'
    scene.render.compositor_device = 'CPU'
    scene.render.compositor_precision = 'FULL'
    scene.render.resolution_x = pixels.shape[1]
    scene.render.resolution_y = pixels.shape[0]
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'OPEN_EXR'
    scene.render.image_settings.color_mode = 'RGBA'
    scene.render.image_settings.color_depth = '32'
    scene.render.image_settings.color_management = 'OVERRIDE'
    scene.render.image_settings.linear_colorspace_settings.name = 'Linear Rec.709'
    # A source-only group has no phantom scene input that could request a camera.
    source = scene.compositor_effects.new('Real Photo Input')
    tree = bpy.data.node_groups.new('Real Photo Source', 'CompositorNodeTree')
    tree.interface.new_socket(name='Image', in_out='OUTPUT', socket_type='NodeSocketColor')
    source.node_group = tree
    output = tree.nodes.new('NodeGroupOutput')
    image = bpy.data.images.load(str(folder / 'input-film.exr'), check_existing=False)
    image.colorspace_settings.name = 'Linear Rec.709'
    image.alpha_mode = 'PREMUL'
    image_node = source.node_group.nodes.new('CompositorNodeImage')
    image_node.image = image
    tree.links.new(image_node.outputs['Image'], output.inputs['Image'])
    effect = effects.create_effect(scene, levels.TYPE)
    node = effects.effect_node(effect)
    assert node.auto_level
    scene.render.filepath = str(folder / 'native-corrected.exr')
    assert bpy.ops.render.render(scene=scene.name, write_still=True) == {'FINISHED'}
    return read(scene.render.filepath)


def run(args, addon, folder):
    folder.mkdir(parents=True, exist_ok=True)
    if args.reuse_input:
        pixels = read(args.reuse_input)
        if args.reuse_input.resolve() != (folder / 'input-film.exr').resolve():
            write(folder / 'input-film.exr', pixels)
    else:
        assert args.raw and Path(args.raw).is_file(), 'Set PHOTO_LEVELS_RAW or supply --raw / --reuse-input'
        pixels = film_input(addon, Path(args.raw), folder, args.edge)
    rgb = pixels[..., :3]
    assert np.isfinite(pixels).all() and np.all(pixels[..., 3] == 1)
    low, high = float(rgb.min()), float(rgb.max())
    assert high > low
    old = pixels.copy()
    old[..., :3] = np.clip((rgb - low) / (high - low), 0, 1)
    black, white = float(np.clip(low, 0, 1)), float(np.clip(high, 0, 1))
    corrected = pixels.copy()
    if high > low and white > black:
        corrected[..., :3] = np.clip((rgb - black) / (white - black), 0, 1)
    visible = np.clip(rgb, 0, 1)
    dark = np.max(visible, axis=2) < .005
    stats = dict(input_minimum=low, input_maximum=high,
                 input_rgb_quantiles=np.quantile(rgb, [0, .001, .01, .5, .99, .999, 1]).tolist(),
                 old_black_zero_maps_to=float(np.clip(-low / (high - low), 0, 1)),
                 corrected_black_point=black, corrected_white_point=white,
                 corrected_black_zero_maps_to=float(np.clip(-black / (white - black), 0, 1)) if white > black else 0.,
                 old_interior_gain=1. / (high - low),
                 corrected_interior_gain=1. / (white - black) if white > black else 1.,
                 dark_pixel_count=int(np.count_nonzero(dark)),
                 original_dark_mean=float(visible[dark].mean()) if np.any(dark) else None,
                 old_dark_mean=float(old[..., :3][dark].mean()) if np.any(dark) else None,
                 corrected_dark_mean=float(corrected[..., :3][dark].mean()) if np.any(dark) else None)
    for name, values in (('before', pixels), ('beta7-old', old), ('corrected', corrected)):
        write(folder / (name + '.png'), values, display=True)
    if not args.record_only:
        actual = native_result(addon, pixels, folder)
        np.testing.assert_allclose(actual, corrected, atol=4e-6, rtol=2e-5)
        np.testing.assert_array_equal(actual[..., 3], pixels[..., 3])
        if white > black:
            assert black >= 0 and white <= 1 and stats['corrected_interior_gain'] >= 1
            assert stats['corrected_black_zero_maps_to'] == 0
            # The same affine map acts on every channel, preserving channel
            # differences up to one shared gain wherever values do not clip.
            keep = np.all((rgb > black) & (rgb < white), axis=2)
            assert np.any(keep)
            np.testing.assert_allclose((actual[..., 0] - actual[..., 1])[keep],
                                       ((rgb[..., 0] - rgb[..., 1]) / (white - black))[keep],
                                       atol=4e-6, rtol=2e-5)
        stats['native_max_absolute_error'] = float(np.max(np.abs(actual - corrected)))
        print('PASS real-photo native Auto Level: black anchor, shared RGB gain, actual output and alpha', flush=True)
    else:
        stats['status'] = 'Input and former-formula evidence only; corrected native graph not tested'
    (folder / 'report.json').write_text(json.dumps(stats, indent=2), encoding='utf-8')
    print(json.dumps(stats, indent=2), flush=True)
    print('LOCAL_PRIVATE_PHOTO_EVIDENCE', folder, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installed', action='store_true')
    parser.add_argument('--raw', default=os.environ.get('PHOTO_LEVELS_RAW'))
    parser.add_argument('--reuse-input', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--record-only', action='store_true')
    parser.add_argument('--edge', type=int, default=1600)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else [])
    if args.installed:
        from bl_ext.user_default import raw_photo_editor as addon
    else:
        existing = sys.modules.get('bl_ext.user_default.raw_photo_editor')
        if existing and hasattr(bpy.types.Scene, 'blender_raw'):
            existing.unregister()
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import blender_raw as addon
        addon.register()
    if args.output:
        run(args, addon, args.output.resolve())
    else:
        with TemporaryDirectory(prefix='levels-private-photo-') as folder:
            run(args, addon, Path(folder))


if __name__ == '__main__':
    main()
