"""Check the installed BaseLook with -- EOS-R5.CR3 Leica-M8.DNG [D700.NEF]."""
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
import OpenImageIO as oiio
import pyexiv2

from bl_ext.user_default.raw_photo_editor import base_look, camera_profiles, raw_backend


def check_gray():
    levels = np.r_[[-.2, 0], np.geomspace(1e-6, 32, 4096)].astype(np.float32)
    gray = np.repeat(levels[:, None], 3, axis=-1)
    rendered = base_look.apply(gray)
    assert rendered.dtype == np.float32 and np.isfinite(rendered).all()
    np.testing.assert_array_equal(rendered[:, 0], rendered[:, 1])
    np.testing.assert_array_equal(rendered[:, 1], rendered[:, 2])
    assert (np.diff(rendered[:, 0]) > 0).all() and rendered[-1, 0] > 10
    assert rendered[0, 0] < 0 and rendered[1, 0] == 0
    middle_gray = np.full((1, 3), .18, np.float32)
    assert .18 < base_look.apply(middle_gray)[0, 0] < .25


def develop(photo, target, **settings):
    raw_backend.decode(photo, target, settings)
    rgb = oiio.ImageBuf(str(target)).get_pixels(oiio.FLOAT)
    assert rgb.ndim == 3 and rgb.shape[2] == 3 and np.isfinite(rgb).all()
    return rgb


def check_capture_white():
    calibration = {50721: np.eye(3), 50722: np.diag([1.5, .8, 1.2]), 50778: [17], 50779: [21]}
    xy = np.array([.325, .35])
    white = np.r_[xy, 1 - xy.sum()] / xy[1]
    # Select the matrix at the recorded illuminant, then derive the neutral
    # from this matrix. The alternate DNG representation must white-balance it.
    settings = {'camera_input': 'AUTO'}
    matrix, weight = raw_backend._camera_transform([0, 1, 0], {50729: xy}, calibration, settings, None)
    individual = calibration[50721] * (1 - weight) + calibration[50722] * weight
    neutral = individual @ white
    neutral /= neutral[1]
    np.testing.assert_allclose(matrix @ neutral, [1, 1, 1], atol=3e-6)
    wb, _ = raw_backend._camera_transform([0, 1, 0], {50729: xy}, calibration, settings, None, wb_only=True)
    np.testing.assert_allclose(wb @ neutral, [1, 1, 1], atol=1e-6)


check_gray()
check_capture_white()
canon, leica, *nikon = map(Path, sys.argv[sys.argv.index('--') + 1:])
assert raw_backend._decoder_matrix(canon) is not None, "The bundled LibRaw matrix bridge is unavailable"
with TemporaryDirectory(prefix='photo-editor-look-') as tmp:
    folder = Path(tmp)
    target = folder / 'render.exr'
    dcp = canon.parent / 'Canon EOS R5.dcp'
    # An unlisted camera and a body that has a legacy DCP follow the same
    # automatic path. A custom DCP changes only the input stage in this test.
    with patch.object(camera_profiles, 'match', side_effect=AssertionError('Unexpected catalog lookup')):
        for photo, kind, extra, name in (
                (leica, 'AUTO', {}, 'Embedded DNG calibration'),
                (canon, 'AUTO', {}, 'LibRaw calibration'),
                (canon, 'DCP', {'profile_path': str(dcp)}, 'Custom DCP')):
            settings = dict(camera_input=kind, preview=True, **extra)
            linear = develop(photo, target, look='NONE', **settings)
            looked = develop(photo, target, look='BASELOOK', **settings)
            np.testing.assert_allclose(looked, base_look.apply(linear), rtol=3e-6, atol=5e-7)
            assert raw_backend.camera_input_name(target) == name
            print('BaseLook', photo.name, name, flush=True)

    if nikon:
        # This DCP has separate calibration tables, an appearance table and
        # a non-identity tone curve. Each selected look should run only once.
        profile = nikon[0].parent / 'NIKON D700.dcp'
        settings = dict(camera_input='DCP', profile_path=str(profile), preview=True)
        linear = develop(nikon[0], target, look='NONE', **settings)
        standard = develop(nikon[0], target, look='PROFILE', **settings)
        base = develop(nikon[0], target, look='BASELOOK', **settings)
        np.testing.assert_allclose(base, base_look.apply(linear), rtol=3e-6, atol=5e-7)
        assert np.mean(np.abs(standard - linear)) > 1e-4
        assert np.mean(np.abs(standard - base)) > 1e-4
        print('Custom DCP calibration and appearance selected independently', flush=True)

    # Alter only recorded baseline exposure on private copies of the fixture.
    zero, plus = folder / 'zero.dng', folder / 'plus.dng'
    for source, value in ((zero, '0/1'), (plus, '1/1')):
        shutil.copyfile(leica, source)
        with pyexiv2.Image(str(source)) as photo:
            photo.modify_exif({'Exif.Image.BaselineExposure': value})
    linear = develop(zero, target, camera_input='AUTO', look='NONE', preview=False, preview_percent=25)
    gained = develop(plus, target, camera_input='AUTO', look='NONE', preview=False, preview_percent=25)
    np.testing.assert_allclose(gained, linear * 2, rtol=2e-5, atol=2e-6)
    looked = develop(plus, target, camera_input='AUTO', look='BASELOOK', preview=False, preview_percent=25)
    np.testing.assert_allclose(looked, base_look.apply(gained), rtol=3e-6, atol=5e-7)
    print('DNG baseline +1 EV exactly precedes BaseLook on the full sensor path', flush=True)
