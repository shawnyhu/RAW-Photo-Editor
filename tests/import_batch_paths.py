"""Run in a fresh Blender profile with the built extension enabled."""
from collections import Counter
import gzip
from pathlib import Path
import tempfile
from unittest.mock import patch

import bpy
from bl_ext.user_default.raw_photo_editor import catalog, filters


def run(directory):
    lib = catalog.library(bpy.context)
    assert not lib.photos
    payload = gzip.decompress((Path(__file__).parent / 'fixtures' / 'highlight-chart.dng.gz').read_bytes())
    names = [f'image-{number}.dng' for number in range(24)]
    for name in names:
        (directory / name).write_bytes(payload)

    for name in names[:2]:
        photo, new = catalog.add_photo(bpy.context, directory / name)
        assert new
    # The saved relative link no longer resolves. Its absolute fallback still
    # identifies the first original when a batch includes that file again.
    first = lib.photos[0]
    first.filepath = str(directory / 'moved-away.dng')
    original_uids = [p.uid for p in lib.photos]
    assert lib.active_index == 0
    linked_file = catalog.linked_file
    indexed = []

    def linked(photo):
        indexed.append(photo.uid)
        return linked_file(photo)

    index_import = filters.index_imported_photo
    attempts = Counter()

    def index_photo(photo, index, path):
        attempts[path.name] += 1
        if path.name == names[3] and attempts[path.name] == 1:
            raise ValueError('Simulated per-file import failure')
        return index_import(photo, index, path)

    with patch.object(catalog, 'linked_file', linked), patch.object(filters, 'index_imported_photo', index_photo):
        sequence = [*names, 'missing.dng', names[3], names[-1]]
        result = bpy.ops.blender_raw.import_photos(directory=str(directory), files=[{'name': n} for n in sequence])
        assert result == {'FINISHED'}
    assert Counter(indexed) == Counter(original_uids), indexed
    assert len(lib.photos) == len(names) and {p.name for p in lib.photos} == set(names)
    assert all(p.scene for p in lib.photos) and lib.active_index == 0
    assert [p.uid for p in lib.photos[:2]] == original_uids
    assert attempts[names[3]] == 2

    result = bpy.ops.blender_raw.import_photos(directory=str(directory), files=[{'name': n} for n in names[:2]], pack=True)
    assert result == {'FINISHED'} and len(lib.photos) == len(names)
    assert all(p.packed and p.source_blob['payload'] == payload for p in lib.photos[:2])
    existing, new = catalog.add_photo(bpy.context, directory / names[0])
    assert not new and existing.uid == original_uids[0]


with tempfile.TemporaryDirectory(prefix='photo-batch-import-') as scratch:
    run(Path(scratch))
print('PASS batch import resolves each existing photo once, preserves fallback paths, embedding, duplicates and retries')
