"""Test directory-driven import thumbnails without selecting or importing any original.

blender -b --offline-mode --python-exit-code 1 --python tests/import_preview.py -- PHOTO.CR3 PHOTO.DNG
"""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import os
import shutil
import sys
import time

import bpy
import numpy as np
import OpenImageIO as oiio

from bl_ext.user_default.raw_photo_editor import catalog, import_preview


def settle(session):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        session.pump(session.entries)
        if not session.job and all(f.directory or session.key(f) in session.icons or
                                   session.key(f) in session.failed for f in session.entries):
            return
        time.sleep(.08)
    raise TimeoutError('RAW thumbnails')


sources = [Path(p).resolve() for p in sys.argv[sys.argv.index('--') + 1:]]
assert len(sources) >= 2
with TemporaryDirectory() as root:
    root = Path(root)
    child = root / 'Subfolder'
    child.mkdir()
    for source in sources:
        try:
            os.link(source, root / source.name)
        except OSError:
            shutil.copyfile(source, root / source.name)
    (root / 'notes.txt').write_text('not a RAW')
    (child / 'broken.nef').write_bytes(b'not a RAW')
    params = SimpleNamespace(directory=os.fsencode(root), filter_search='', show_hidden=False,
                             sort_method='FILE_SORT_ALPHA', use_sort_invert=False, recursion_level='NONE')
    session = import_preview._Session()
    scratch = Path(session.folder.name)
    try:
        assert session.refresh(params)
        assert {f.name for f in session.entries} == {'Subfolder', *(p.name for p in sources)}
        settle(session)
        assert not session.failed and len(session.icons) == len(sources)
        for entry in session.entries:
            if entry.directory:
                continue
            with closing(oiio.ImageInput.open(str(session.cache / (session.key(entry) + '.jpg')))) as reader:
                spec = reader.spec()
                pixels = reader.read_image(format=oiio.UINT8)
                assert 0 < min(spec.width,spec.height) <= max(spec.width,spec.height) <= 256
                assert np.ptp(pixels[..., :3]) > 10
        params.filter_search = sources[0].stem
        assert session.refresh(params)
        assert [f.name for f in session.entries] == [sources[0].name]
        params.directory, params.filter_search = os.fsencode(child), ''
        assert session.refresh(params)
        settle(session)
        assert session.key(session.entries[0]) in session.failed
        params.directory = os.fsencode(root)
        assert session.refresh(params)
        settle(session)
        assert not catalog.library(bpy.context).photos
    finally:
        session.close()
    assert not scratch.exists()
    reopened = import_preview._Session()
    try:
        reopened.refresh(params)
        with patch.object(import_preview.jobs, '_start', side_effect=AssertionError('Warm reopen started a worker')):
            settle(reopened)
        assert not reopened.job and len(reopened.icons) == len(sources)
        entry = next(f for f in reopened.entries if f.name == sources[0].name)
        key = reopened.key(entry)
        cached = reopened.cache / (key + '.jpg')
        cached.write_bytes(b'not a JPEG')
    finally:
        reopened.close()
    repaired = import_preview._Session()
    try:
        repaired.refresh(params)
        settle(repaired)
        assert not repaired.failed and cached.read_bytes()[:2] == b'\xff\xd8'
        # Replace the test link via rename so the original's mtime and contents stay intact.
        replacement = root / '.replacement'
        shutil.copyfile(sources[0], replacement)
        os.utime(replacement, ns=(entry.modified + 2_000_000_000,) * 2)
        os.replace(replacement, root / entry.name)
        repaired.scan = None
        repaired.refresh(params)
        new_key = repaired.key(next(f for f in repaired.entries if f.name == entry.name))
        assert new_key != key
        settle(repaired)
        assert new_key in repaired.icons and not repaired.failed
        # Reuse a valid fixture JPEG for a fully warm page larger than the usual 128-icon limit.
        visible = [import_preview.Entry(f'test-{i:03}.cr3', False, 1, 1) for i in range(146)]
        for item in visible:
            shutil.copyfile(cached, repaired.cache / (repaired.key(item) + '.jpg'))
        repaired.pump(visible)
        assert repaired.pending_icons and not repaired.job
        deadline = time.monotonic() + 30
        while repaired.pending_icons and time.monotonic() < deadline:
            repaired.pump(visible)
        assert not repaired.pending_icons and len(repaired.icons) == len(visible)
        assert not repaired.pump(visible) and not repaired.job
        assert all(repaired.key(item) in repaired.icons for item in visible)
    finally:
        repaired.close()
print('PASS RAW grid, corrupt original and cache, warm reopen, source revision and large visible pages')
