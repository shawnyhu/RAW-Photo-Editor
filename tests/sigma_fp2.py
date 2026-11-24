"""Check the installed extension on FP_00082.DNG and FP_00170.DNG (pass both after --)."""
from contextlib import closing
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import bpy
import numpy as np
import OpenImageIO as oiio

from bl_ext.user_default.raw_photo_editor import catalog, compositor, raw_backend, worker


def check(source, folder):
    photo, _ = catalog.add_photo(bpy.context, source)
    tree = compositor.ensure_tree(bpy.context, photo)
    developer = next(n for n in tree.nodes if n.bl_idname == compositor.DEVELOPER_TYPE)
    assert photo.camera_input == developer.camera_input == 'AUTO'
    reader = catalog.request(photo, 'full')
    assert reader == catalog.request(photo, 'full', developer)
    assert reader['settings']['camera_input'] == 'AUTO'
    developer.camera_input = 'APPLE'
    for mode in ('preview', 'full'):
        req = catalog.request(photo, mode, developer)
        path = folder / (source.stem + '-' + mode + '.exr')
        worker.process_image(dict(req, output=str(path)))
        assert raw_backend.camera_input_name(path) == 'Apple RAW (Core Image)'
        with closing(oiio.ImageInput.open(str(path))) as image:
            rgb = image.read_image(format=oiio.FLOAT)[::16, ::16, :3]
            y = rgb @ (0.2126, 0.7152, 0.0722)
            # In 0.15.0 about a fifth of this scene fell below black before display.
            assert np.isfinite(y).all() and np.mean(y <= 0) < .001 and np.percentile(y, 5) > .002
            print(source.name, mode, 'nonpositive luminance', np.mean(y <= 0))


with TemporaryDirectory() as temp:
    for name in sys.argv[sys.argv.index('--') + 1:]:
        check(Path(name).resolve(), Path(temp))
