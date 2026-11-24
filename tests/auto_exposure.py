"""Run with Blender 5.3 in background. Optionally pass a RAW sample directory after --."""

from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

import bpy
import numpy as np
import OpenImageIO as oiio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'blender_raw'))
import metering
import raw_backend


def gray(value):
    return np.full((64, 64, 3), value, np.float32)


assert metering.exposure_ev('AS_SHOT', object()) == 0.0
for level, expected in ((0.18, 0), (0.09, 1), (0.36, -1)):
    assert abs(metering.measure(gray(level))['GLOBAL'] - expected) < 1e-6
for bad in (0.0, -0.1, np.inf, np.nan):
    assert all(v == 0.0 for v in metering.measure(gray(bad)).values())
assert metering.measure(gray(1e-7))['GLOBAL'] == 6.0
assert metering.measure(gray(200))['GLOBAL'] == -6.0

center = gray(0.30)
center[16:48, 16:48] = 0.015
values = metering.measure(center)
assert values['CENTER'] > values['GLOBAL'] + 0.4, values
assert metering.measure(center[::-1]) == values
assert values['CENTER'] - values['GLOBAL'] > 1.8

contrasty = gray(0.18)
contrasty[:12] = 0.003
contrasty[-4:] = (2.0, 0.09, 0.07)
values = metering.measure(contrasty)
assert values['HIGHLIGHTS'] < values['GLOBAL'] < values['SHADOWS'], values
assert abs(2**values['HIGHLIGHTS'] * 2.0 - 0.9) < 1e-6
assert abs(2**values['SHADOWS'] * 0.003 - 0.03) < 1e-6


def check_film_meters(folder):
    # Wide, odd-sized frame with strong edges distinguishes the Mac meter's
    # top-left, aspect-preserving grid from the photographic log-average meter.
    height, width = 277, 605
    y, x = np.mgrid[:height, :width]
    level = np.where((x - 300)**2 + (y - 135)**2 < 7500, 0.7, 0.092)
    level[:, ::4] = 0.01
    rgb = np.repeat(level[:, :, None], 3, axis=2).astype(np.float32)
    ys = np.arange(117) * height // 117
    xs = np.arange(256) * width // 256
    samples = level[ys[:, None], xs]
    wy, wx = np.mgrid[:117, :256]
    weights = np.exp(-((wy - 58.5)**2 + (wx - 128)**2) / (2 * (256 * 0.2)**2))
    expected = {'FILM_MEDIAN': np.log2(0.184 / np.median(samples)),
                'FILM_CENTER': np.log2(0.184 / np.average(samples, weights=weights))}
    for name, header in [('decoded', True), ('legacy', False)]:
        path = folder / (name + '.exr')
        if header:
            raw_backend._write_image(path, rgb, np.eye(3))
        else:
            assert oiio.ImageBuf(rgb).write(str(path))
        image = bpy.data.images.load(str(path))
        try:
            for packed in (False, True):
                if packed:
                    image.pack()
                    path.unlink()
                for mode, value in expected.items():
                    assert abs(metering.exposure_ev(mode, image) - value) < 1e-5
        finally:
            bpy.data.images.remove(image)


def verify_exr(path):
    expected = metering._read_exr(path)
    reader = oiio.ImageInput.open(str(path))
    try:
        spec = reader.spec()
        assert all(spec.getattribute(metering._CENTER_HEADER if key == 'CENTER' else metering._HEADER_PREFIX + key)
                   is not None for key in expected)
        # Read the entire preview independently. Recompute the same metering grid
        # directly on the EXR's output pixels, bypassing the decoder's metadata.
        rgb = reader.read_image(format=oiio.FLOAT)
        y = metering._sample_positions(spec.height)
        x = metering._sample_positions(spec.width)
        measured = metering.measure(rgb[y[:, None], x], (spec.width, spec.height))
    finally:
        reader.close()
    for key in expected:
        assert abs(measured[key] - expected[key]) < 1e-5, (path, key, measured, expected)
    image = bpy.data.images.load(str(path))
    try:
        assert metering.exposure_ev('AS_SHOT', image) == 0.0
        for key in expected:
            assert metering.exposure_ev(key, image) == expected[key]
        image.pack()
        path.unlink()
        # Packing only the reader EXR must retain metering even when taken offline.
        for key in expected:
            assert metering.exposure_ev(key, image) == expected[key]
    finally:
        bpy.data.images.remove(image)
    return expected


def verify_spot(folder):
    y, x = np.mgrid[:80, :160]
    levels = np.where((x-39.5)**2 + (y-19.5)**2 <= 8.5**2, 0.045, 0.36)
    rgb = np.repeat(levels[:, :, None], 3, axis=2).astype(np.float32)
    path = folder / 'spot.exr'
    raw_backend._write_image(path, rgb, np.eye(3))
    image = bpy.data.images.load(str(path))
    try:
        for packed in (False, True):
            if packed:
                image.pack()
                path.unlink()
            for spot, expected in (((0.25, 0.25, 8.), 2.), ((0.25, 0.75, 8.), -1.), ((1., 0., 12.), -1.)):
                assert abs(metering.exposure_ev('SPOT', image, spot=spot) - expected) < 1e-5
            assert -1 < metering.exposure_ev('SPOT', image, spot=(.25, .25, 20.)) < 0
    finally:
        bpy.data.images.remove(image)
    assert not any(entry[0] in metering._FILM_MODES for entry in metering.mode_items(SimpleNamespace(auto_exposure='SPOT'), None))
    assert metering.mode_items(SimpleNamespace(auto_exposure='FILM_MEDIAN'), None)[-1][:2] == ('FILM_MEDIAN', 'Saved Median')


with tempfile.TemporaryDirectory(prefix='photo-editor-ae-') as folder:
    folder = Path(folder)
    verify_spot(folder)
    check_film_meters(folder)
    synthetic = folder / 'synthetic.exr'
    raw_backend._write_image(synthetic, contrasty, np.eye(3))
    verify_exr(synthetic)

    legacy = folder / 'old.exr'
    image = oiio.ImageBuf(contrasty)
    assert image.write(str(legacy)), image.geterror()
    for key, ev in metering._read_exr(legacy).items():
        assert abs(ev - values[key]) < 1e-6, (key, ev, values)

    if '--' in sys.argv:
        source_dir = Path(sys.argv[sys.argv.index('--') + 1])
        for suffix in ('dng', 'arw', 'cr3', 'nef'):
            source = next(p for p in sorted(source_dir.iterdir()) if p.suffix.lower() == '.' + suffix)
            output = folder / (source.stem + '.exr')
            raw_backend.decode(source, output, {'preview': True})
            offsets = verify_exr(output)
            print('METER', source.name, {k: round(v, 3) for k, v in offsets.items()}, flush=True)
print('AUTO EXPOSURE PASSED', flush=True)
