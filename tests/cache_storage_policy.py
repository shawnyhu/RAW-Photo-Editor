"""Verify cache packing and externalization in Blender 5.3 with isolated, saved projects."""
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

import bpy
import numpy as np
import OpenImageIO as oiio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from blender_raw import catalog, compositor, jobs


def write_exr(path, seed):
    pixels = np.random.default_rng(seed).random((320, 384, 3), dtype=np.float32)
    writer = oiio.ImageOutput.create(str(path))
    assert writer.open(str(path), oiio.ImageSpec(384, 320, 3, oiio.FLOAT))
    assert writer.write_image(pixels)
    assert writer.close()


def records():
    lib = catalog.library(bpy.context)
    photo = lib.photos[0]
    dev = next(n for n in photo.tree.nodes if n.bl_idname == compositor.DEVELOPER_TYPE)
    return lib, photo, dev, photo.image, dev.node_tree.nodes["Developed RAW"].image


def run(root):
    catalog.register()
    compositor.register()
    jobs.register()
    lib = catalog.library(bpy.context)
    assert not bpy.data.use_autopack and not lib.pack_cache_images
    lib.owner = True
    lib.preview_resolution = "100"
    source = root / "camera.dng"
    source.write_bytes(b"original bytes")
    profile = root / "camera-profile.dcp"
    profile.write_bytes(b"profile bytes")
    photo = lib.photos.add()
    photo.filepath, photo.name, photo.uid = str(source), source.name, "0.7-pack-test"
    compositor.ensure_tree(bpy.context, photo)
    dev = next(n for n in photo.tree.nodes if n.bl_idname == compositor.DEVELOPER_TYPE)
    dev.profile_mode, dev.profile_path = "CUSTOM", str(profile)
    catalog.pack_photo(photo)
    base_req = catalog.request(photo, "full")
    dev_req = catalog.request(photo, "full", recipe=dev)
    for i, req in enumerate((base_req, dev_req)):
        write_exr(req["output"], i)
    catalog.apply_image(photo, base_req)
    catalog.apply_developer_image(photo, dev, dev_req)
    # This ordinary image has a hash-shaped EXR filename and must never be managed.
    extra_path = root / ("f" * 64 + ".exr")
    write_exr(extra_path, 3)
    extra = bpy.data.images.load(str(extra_path))
    extra.name, extra.use_fake_user = "Other asset", True
    compact = root / "default.blend"
    assert bpy.ops.wm.save_as_mainfile(filepath=str(compact), compress=False) == {"FINISHED"}
    assert not photo.image.packed_file and not dev.node_tree.nodes["Developed RAW"].image.packed_file
    assert not extra.packed_file

    lib.pack_cache_images = True
    assert not bpy.data.use_autopack
    packed = root / "packed.blend"
    assert bpy.ops.wm.save_as_mainfile(filepath=str(packed), compress=False) == {"FINISHED"}
    assert bpy.ops.wm.open_mainfile(filepath=str(packed)) == {"FINISHED"}
    lib, photo, dev, base, variant = records()
    assert lib.pack_cache_images and not bpy.data.use_autopack
    assert base.packed_file and variant.packed_file and not bpy.data.images["Other asset"].packed_file
    bytes_added = base.packed_file.size + variant.packed_file.size
    assert packed.stat().st_size > compact.stat().st_size + bytes_added // 2
    assert photo.source_blob["payload"] == source.read_bytes()
    assert dev.profile_blob["payload"] == profile.read_bytes()

    # Native pack-all stays effective. Switching the Gallery setting off then
    # affects only its caches, even when a disk copy is missing or different.
    assert bpy.ops.file.pack_all() == {"FINISHED"}
    bpy.data.use_autopack = True
    base_bytes = base.packed_file.data
    variant_bytes = variant.packed_file.data
    base_path, variant_path = Path(base_req["output"]), Path(dev_req["output"])
    assert base_path.resolve().is_relative_to(root.resolve())
    assert variant_path.resolve().is_relative_to(root.resolve())
    base_path.unlink()
    variant_path.write_bytes(b"keep this preexisting file")
    lib.pack_cache_images = False
    assert not lib.cache_storage_error, lib.cache_storage_error
    assert not lib.pack_cache_images and not bpy.data.use_autopack
    assert not base.packed_file and not variant.packed_file
    assert bpy.data.images["Other asset"].packed_file
    assert base_path.read_bytes() == base_bytes
    assert Path(variant.filepath).read_bytes() == variant_bytes
    assert variant_path.read_bytes() == b"keep this preexisting file"
    assert Path(catalog.request(photo, "full", recipe=dev)["output"]).read_bytes() == variant_bytes
    assert photo.source_blob["payload"] == source.read_bytes() and dev.profile_blob["payload"] == profile.read_bytes()
    stored = root / "external.blend"
    assert bpy.ops.wm.save_as_mainfile(filepath=str(stored), compress=False) == {"FINISHED"}
    bpy.ops.wm.open_mainfile(filepath=str(stored))
    lib, photo, dev, base, variant = records()
    assert not lib.pack_cache_images and not base.packed_file and not variant.packed_file
    assert bpy.data.images["Other asset"].packed_file

    # Old projects and explicit manual packs keep exactly the subset packed.
    base.pack()
    del base[catalog._CACHE_KEY]
    del variant[catalog._CACHE_KEY]
    legacy = root / "legacy.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(legacy), compress=False)
    bpy.ops.wm.open_mainfile(filepath=str(legacy))
    lib, photo, dev, base, variant = records()
    assert not lib.pack_cache_images and base.packed_file and not variant.packed_file
    assert catalog.has_packed_cache_images()
    assert bpy.ops.blender_raw.externalize_cache_images() == {"FINISHED"}
    assert not catalog.has_packed_cache_images()
    assert bpy.data.images["Other asset"].packed_file

    bpy.data.use_autopack = True
    assert lib.pack_cache_images
    auto = root / "native.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(auto), compress=False)
    bpy.ops.wm.open_mainfile(filepath=str(auto))
    lib, photo, dev, base, variant = records()
    assert lib.pack_cache_images and bpy.data.use_autopack
    assert base.packed_file and variant.packed_file
    # A write failure must leave the stored bytes available and expose a retry.
    with patch.object(catalog, "_matches_payload", side_effect=OSError("Cannot access cache")):
        lib.pack_cache_images = False
    assert base.packed_file and variant.packed_file and "Cannot access cache" in lib.cache_storage_error
    assert bpy.ops.blender_raw.externalize_cache_images() == {"FINISHED"}
    assert not base.packed_file and not variant.packed_file and not lib.cache_storage_error
    print("Cache storage policy passed:", compact.stat().st_size, "bytes default,", packed.stat().st_size, "packed", flush=True)


with tempfile.TemporaryDirectory(prefix="photo-editor-storage-policy-") as folder:
    with patch.object(tempfile, "tempdir", folder):
        run(Path(folder))
