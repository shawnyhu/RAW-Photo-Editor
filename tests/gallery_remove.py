"""Remove Gallery entries while a real export and RAW previews are running.

blender -b --offline-mode --python-exit-code 1 --python tests/gallery_remove.py -- Leica-M8.DNG
"""
from hashlib import sha256
from pathlib import Path
import shutil
import sys
import tempfile
import time

import bpy
import OpenImageIO as oiio
from bl_ext.user_default.raw_photo_editor import catalog, compositor, jobs


def settle(predicate):
    deadline = time.monotonic() + 180
    while not predicate():
        assert time.monotonic() < deadline, "Gallery jobs did not finish"
        jobs.tick()
        time.sleep(.03)


def select(lib, *uids):
    for photo in lib.photos:
        photo.selected = photo.uid in uids


def start_export(folder):
    folder.mkdir()
    assert bpy.ops.blender_raw.export_photos(directory=str(folder), format="JPEG") == {'FINISHED'}
    assert jobs.export_running()


def finish_export():
    settle(lambda: not jobs.export_running())
    assert not jobs.export_details(), jobs.export_details()


with tempfile.TemporaryDirectory(prefix="Gallery-remove-") as tmp:
    root = Path(tmp)
    source = Path(sys.argv[-1])
    assets = [root / (name + source.suffix) for name in ('A', 'B', 'C', 'D')]
    for asset in assets:
        shutil.copyfile(source, asset)
        assert bpy.ops.blender_raw.import_photos(filepath=str(asset)) == {'FINISHED'}
    lib = catalog.library(bpy.context)
    a, b, c, d = (p.uid for p in lib.photos)
    select(lib)
    assert bpy.ops.blender_raw.remove_photo() == {'CANCELLED'}
    assert bpy.ops.blender_raw.remove_photo(uid="missing") == {'CANCELLED'}
    assert bpy.ops.blender_raw.open_photo(uid=b) == {'FINISHED'}
    settle(lambda: not jobs.developing(b))
    scene = bpy.context.scene
    assert scene.get(compositor.PHOTO_TAG) == b
    select(lib, b)
    start_export(root / 'First')
    finish_export()
    existing = root / 'First' / 'B.jpg'
    existing_hash = sha256(existing.read_bytes()).digest()

    select(lib, b, c)
    start_export(root / 'Running')
    jobs.stop_previews()
    # Use two uncached workers and a queued request to exercise selective cancellation.
    jobs.preview_later(lib.photos[0], immediate=True)
    jobs.preview_later(lib.photos[3], immediate=True)
    jobs.developer_later(lib.photos[1], next(compositor.developers_for(lib.photos[1])))
    jobs.tick()
    assert {key[0] for key in jobs._active} == {a, d}, jobs._active.keys()
    proc = next(proc for key, (proc, _, _) in jobs._active.items() if key[0] == a)
    select(lib, a, b)
    assert bpy.ops.blender_raw.remove_photo() == {'FINISHED'}
    assert [p.uid for p in lib.photos] == [c, d] and lib.active_index == 0
    assert not jobs.developing(a) and not jobs.developing(b) and proc.poll() is not None
    assert jobs.developing(d) and jobs.export_running()
    assert scene.get(compositor.PHOTO_TAG) == c and scene.compositor_effects["Photo"].node_group == lib.photos[0].tree
    finish_export()
    for name in ('B.jpg', 'C.jpg'):
        image = oiio.ImageInput.open(str(root / 'Running' / name))
        assert image and image.spec().width > 1600
        image.close()
    # Preserve the scripted single-UID operation independently of selection.
    select(lib, c)
    assert bpy.ops.blender_raw.remove_photo(uid=d) == {'FINISHED'}
    assert lib.photos[0].uid == c and lib.photos[0].selected and lib.active_index == 0
    assert bpy.ops.blender_raw.remove_photo() == {'FINISHED'}
    assert not lib.photos and lib.active_index == -1 and not scene.get(compositor.PHOTO_TAG)
    assert not scene.compositor_effects.get("Photo")
    assert sha256(existing.read_bytes()).digest() == existing_hash
    source_hash = sha256(source.read_bytes()).digest()
    assert all(sha256(p.read_bytes()).digest() == source_hash for p in assets)
    print("Gallery removal passed: new and exported photos, active previews, snapshot export, disk files and empty darkroom")
