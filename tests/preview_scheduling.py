"""Exercise an interactive Reader and Developer with a slow 300-photo thumbnail queue.

blender -b --factory-startup --offline-mode --python-exit-code 1 -P tests/preview_scheduling.py
"""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import bpy
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import blender_raw
from blender_raw import catalog, jobs, native_effects, raw_backend


def run(folder):
    lib = catalog.library(bpy.context)
    lib.owner = True
    for index in range(300):
        photo = lib.photos.add()
        photo.uid = str(index)
        photo.name = f'photo-{index}.dng'
        photo.filepath = str(folder / photo.name)
        Path(photo.filepath).touch()
        jobs.thumbnail_later(photo)
    selected, previous = lib.photos[152], lib.photos[51]
    now = 100.
    starts = []

    def start(req):
        assert len(jobs._active) < 2
        if req['mode'] == 'thumb':
            assert not any(key[1] == 'thumb' for key in jobs._active)
        process = Mock(returncode=None)
        process.poll.side_effect = lambda: process.returncode
        starts.append((req, process))
        return process, Mock()

    def finish_development():
        request, process = starts[-1]
        assert request['mode'] == 'preview' and Path(request['source']).samefile(selected.filepath), request
        raw_backend._write_image(request['output'], np.full((4, 4, 3), .25, dtype=np.float32))
        process.returncode = 0
        jobs.tick()

    with (patch.object(jobs, '_start', side_effect=start),
          patch.object(jobs, 'time', SimpleNamespace(monotonic=lambda: now)),
          patch.object(catalog, 'cache_dir', return_value=folder)):
        jobs.tick()
        jobs.tick()
        assert [r['mode'] for r, _ in starts] == ['thumb']

        assert bpy.ops.blender_raw.open_photo(uid=previous.uid) == {'FINISHED'}
        assert bpy.ops.blender_raw.open_photo(uid=selected.uid) == {'FINISHED'}
        developer = native_effects.effect_node(selected.scene.compositor_effects[1])
        developer.white_balance, developer.temperature = 'CUSTOM', 7200.
        jobs.tick()
        assert len(starts) == 2 and jobs.preview_phase(previous) == 'QUEUED'
        assert jobs.preview_phase(selected) == 'PROCESSING'
        finish_development()
        assert jobs.preview_phase(selected, developer) == 'QUEUED'
        assert jobs.preview_phase(previous) == 'QUEUED'
        now += 1.
        jobs.tick()
        recipe = starts[-1][0]['settings']
        assert recipe['white_balance'] == 'CUSTOM' and recipe['temperature'] == 7200.
        finish_development()
        assert not jobs.developing(selected.uid)
        assert developer.node_tree.nodes['Developed RAW'].image
        assert starts[0][1].poll() is None  # The first thumbnail is still decoding.

        jobs.stop_previews({previous.uid, '1'})
        assert not jobs.developing(previous.uid)
        thumb, process = starts[0]
        Path(thumb['output']).write_bytes(b'camera thumbnail')
        process.returncode = 0
        jobs.tick()
        next_thumb = starts[-1][0]
        assert next_thumb['mode'] == 'thumb' and Path(next_thumb['source']).samefile(lib.photos[2].filepath)
        jobs.stop_previews()
        count = len(starts)
        jobs.tick()
        assert len(starts) == count


blender_raw.register()
try:
    with TemporaryDirectory() as directory:
        run(Path(directory))
finally:
    blender_raw.unregister()
print('PASS active RAW Reader and Developer, switch priority, bounded workers and thumbnail cancellation')
