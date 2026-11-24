"""Exercise the installed camera ICC engine, optionally through the RAW decoder.

blender -b --offline-mode --python-exit-code 1 --python tests/input_icc.py -- [PHOTO.DNG]
The matrix/TRC and A2B LUT profiles are generated here and remain local.
"""
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import time

import numpy as np
from bl_ext.user_default.raw_photo_editor import input_icc


# Test primaries are the standard D50-adapted linear Rec.709 colorants.
_PRIMARIES = np.array(((.436075, .222505, .013932),
                       (.385065, .716878, .097105),
                       (.143080, .060617, .714173)))


def camera_profile(path, gamma=1.0, lut=False, device=b'scnr', space=b'RGB ', version=2):
    def xyz(v):
        return b'XYZ ' + bytes(4) + struct.pack('>iii', *(round(n * 65536) for n in v))

    header = bytearray(128)
    header[8:12] = bytes((version, 0x10, 0, 0))
    header[12:24] = device + space + b'XYZ '
    header[24:36] = struct.pack('>6H', 2026, 1, 5, 12, 0, 0)
    header[36:40] = b'acsp'
    header[68:80] = xyz((.9642, 1.0, .8249))[8:]
    tags = {b'wtpt': xyz((.9642, 1.0, .8249))}
    for tag, text in ((b'desc', 'Photo Editor test camera'), (b'cprt', 'RAW Photo Editor contributors')):
        if version == 4:
            encoded = text.encode('utf-16-be')
            tags[tag] = b'mluc' + bytes(4) + struct.pack('>II4sII', 1, 12, b'enUS', len(encoded), 28) + encoded
        elif tag == b'desc':
            encoded = text.encode('ascii') + bytes(1)
            tags[tag] = b'desc' + bytes(4) + struct.pack('>I', len(encoded)) + encoded + bytes(78)
        else:
            tags[tag] = b'text' + bytes(4) + text.encode('ascii') + bytes(1)
    if lut:
        # lut16 input curves, 2x2x2 color lookup table, output curves. In a
        # 16-bit XYZ PCS 0x8000 represents 1.0. The blue coordinate varies fastest.
        grid = bytearray()
        for r in (0, 1):
            for g in (0, 1):
                for b in (0, 1):
                    point = np.array((r, g, b)) @ _PRIMARIES
                    grid += struct.pack('>3H', *(round(v * 32768) for v in point))
        endpoints = struct.pack('>2H', 0, 65535) * 3
        matrix = struct.pack('>9i', 65536, 0, 0, 0, 65536, 0, 0, 0, 65536)
        tags[b'A2B0'] = (b'mft2' + bytes(4) + bytes((3, 3, 2, 0)) + matrix
                        + struct.pack('>HH', 2, 2) + endpoints + grid + endpoints)
    else:
        curve = b'curv' + bytes(4) + struct.pack('>IH', 1, round(gamma * 256))
        for key, primary in zip((b'r', b'g', b'b'), _PRIMARIES):
            tags[key + b'XYZ'] = xyz(primary)
            tags[key + b'TRC'] = curve

    directory = bytearray(struct.pack('>I', len(tags)))
    payload = bytearray()
    start = 132 + 12 * len(tags)
    for tag, value in tags.items():
        directory += tag + struct.pack('>II', start + len(payload), len(value))
        payload += value + bytes(-len(value) % 4)
    header[:4] = struct.pack('>I', 128 + len(directory) + len(payload))
    path.write_bytes(header + directory + payload)
    return path


def gallery_roundtrip(folder, raw_path, linear, nonlinear, cube, references):
    import bpy
    import OpenImageIO as oiio
    from bl_ext.user_default.raw_photo_editor import catalog, compositor, jobs, raw_backend

    def wait(predicate):
        deadline = time.monotonic() + 300
        while not predicate():
            assert time.monotonic() < deadline, "Gallery job timed out"
            jobs.tick()
            time.sleep(.05)

    def sampled_exr(path):
        image = oiio.ImageBuf(str(path))
        w, h = image.spec().width, image.spec().height
        assert w > 1 and h > 1, image.geterror()
        return (w, h), np.asarray([image.getpixel(x, y)[:3]
                for y in np.linspace(0, h - 1, 16, dtype=int)
                for x in np.linspace(0, w - 1, 16, dtype=int)])

    local_raw = folder / ("Gallery" + Path(raw_path).suffix)
    shutil.copyfile(raw_path, local_raw)
    assert bpy.ops.blender_raw.import_photos(filepath=str(local_raw)) == {"FINISHED"}
    photo, = catalog.library(bpy.context).photos
    assert bpy.ops.blender_raw.open_photo(uid=photo.uid) == {"FINISHED"}
    node, = compositor.developers_for(photo)
    node.camera_input = "ICC"
    node.look = "NONE"
    keys = []
    for name, path in (("linear", linear), ("quadratic", nonlinear), ("linear", linear)):
        node.profile_path = str(path)
        assert bpy.ops.blender_raw.open_photo(uid=photo.uid) == {"FINISHED"}
        wait(lambda: not jobs.developing(photo.uid))
        assert not photo.error and not node.error, (photo.error, node.error)
        image = node.node_tree.nodes["Developed RAW"].image
        assert image and image.get("photo_editor_profile") == "Custom ICC"
        key = catalog.request(photo, "preview", recipe=node)["key"]
        assert Path(image.filepath).stem == key
        keys.append(key)
        preview = oiio.ImageBuf(bpy.path.abspath(image.filepath)).get_pixels(oiio.FLOAT)
        np.testing.assert_allclose(preview[::32, ::32], references[name][::32, ::32], rtol=3e-5, atol=3e-6)
    assert keys[0] == keys[2] and keys[0] != keys[1]

    bpy.ops.wm.save_as_mainfile(filepath=str(folder / "Linked camera ICC and creative LUT.blend"))
    node.look = "LUT"
    node.lut_path = bpy.path.relpath(str(cube))
    assert node.lut_space == "SRGB"
    assert bpy.ops.blender_raw.open_photo(uid=photo.uid) == {"FINISHED"}
    wait(lambda: not jobs.developing(photo.uid))
    assert not node.error, node.error
    variant = node.node_tree.nodes["Developed RAW"].image
    preview = oiio.ImageBuf(bpy.path.abspath(variant.filepath)).get_pixels(oiio.FLOAT)
    np.testing.assert_allclose(preview[::32, ::32], references["look"][::32, ::32], rtol=3e-5, atol=3e-6)
    srgb_key = catalog.request(photo, "preview", recipe=node)["key"]
    node.lut_space = "LINEAR"
    linear_key = catalog.request(photo, "preview", recipe=node)["key"]
    assert linear_key != srgb_key
    cube.write_bytes(cube.read_bytes() + b"\n# Same path, updated contents\n")
    assert catalog.request(photo, "preview", recipe=node)["key"] != linear_key
    node.profile_path = str(nonlinear)
    photo.selected = True
    settings = photo.scene.render.image_settings
    settings.file_format, settings.color_depth = "OPEN_EXR", "32"
    linked_dest = folder / "linked-export"
    assert bpy.ops.blender_raw.export_photos(directory=str(linked_dest)) == {"FINISHED"}
    wait(lambda: not jobs.export_running())
    assert not jobs.export_details(), jobs.export_details()
    wanted = nonlinear.read_bytes()
    wanted_lut = cube.read_bytes()
    catalog.pack_photo(photo)
    assert photo.source_blob and node.profile_blob and node.lut_blob
    project = folder / "Embedded camera ICC and creative LUT.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(project))
    local_raw.unlink()
    nonlinear.unlink()
    cube.unlink()
    bpy.ops.wm.open_mainfile(filepath=str(project))
    photo, = catalog.library(bpy.context).photos
    node, = compositor.developers_for(photo)
    assert (node.camera_input, node.look, node.lut_space) == ("ICC", "LUT", "LINEAR")
    assert catalog.profile_file(node).read_bytes() == wanted
    assert catalog.source_file(photo).is_file() and not local_raw.exists()
    restored_lut = Path(catalog.request(photo, "full", recipe=node)["settings"]["lut_path"])
    assert restored_lut.read_bytes() == wanted_lut

    photo.selected = True
    dest = folder / "export"
    assert bpy.ops.blender_raw.export_photos(directory=str(dest)) == {"FINISHED"}
    wait(lambda: not jobs.export_running())
    assert not jobs.export_details(), jobs.export_details()
    # Independently apply the known LUT to the full-size ICC-only EXR.
    expected = folder / "expected-full.exr"
    raw_backend.decode(catalog.source_file(photo), expected, {
        "camera_input": "ICC", "look": "NONE", "profile_path": str(catalog.profile_file(node))})
    size, actual = sampled_exr(dest / "Gallery.exr")
    reference_size, reference = sampled_exr(expected)
    assert size == reference_size
    in_range = ((reference >= 0) & (reference <= 1)).all(axis=-1)
    assert reference[in_range, 1].max() > .01
    np.testing.assert_allclose(actual[in_range], reference[in_range] * [1, .5, 1], rtol=4e-5, atol=4e-6)
    linked_size, linked_pixels = sampled_exr(linked_dest / "Gallery.exr")
    assert linked_size == size
    np.testing.assert_allclose(actual, linked_pixels, rtol=4e-5, atol=4e-6)
    print("Packed RAW, ICC and .cube reopened after removing all three original files. Full EXR pixels match", size, flush=True)

    node.lut_path = str(folder / "missing.cube")
    assert not node.lut_blob
    broken = folder / "failed-export"
    assert bpy.ops.blender_raw.export_photos(directory=str(broken)) == {"FINISHED"}
    wait(lambda: not jobs.export_running())
    assert any("missing.cube" in message for message in jobs.export_details()), jobs.export_details()
    assert not (broken / "Gallery.exr").exists()


def run(folder, raw_path=None):
    linear = camera_profile(folder / 'linear camera ü.icc')
    nonlinear = camera_profile(folder / 'quadratic camera.icm', gamma=2)
    lut = camera_profile(folder / 'camera LUT.icc', lut=True, version=4)
    samples = np.array([[[0, 0, 0], [.5, .5, .5], [.2, .3, .8], [1, 1, 1],
                         [-.05, .1, .25], [2, 1.5, .5]]], dtype=np.float32)
    for path in (linear, lut):
        with input_icc.open_transform(path) as convert:
            np.testing.assert_allclose(convert(samples), samples, atol=.001, rtol=0)
    with input_icc.open_transform(nonlinear) as convert:
        np.testing.assert_allclose(convert(samples[:, :4]), samples[:, :4]**2, atol=.0005, rtol=0)
    invalid = folder / 'invalid.icc'
    for data in (b'broken', linear.read_bytes()[:-32],
                 camera_profile(invalid, device=b'mntr').read_bytes(),
                 camera_profile(invalid, device=b'prtr').read_bytes(),
                 camera_profile(invalid, space=b'CMYK').read_bytes()):
        invalid.write_bytes(data)
        try:
            with input_icc.open_transform(invalid):
                raise AssertionError('Accepted an incompatible ICC profile')
        except ValueError:
            pass
    print('ICC matrix/TRC, A2B LUT, Unicode path, extended range and invalid profiles passed', flush=True)
    if raw_path:
        import OpenImageIO as oiio
        from bl_ext.user_default.raw_photo_editor import raw_backend
        cube = folder / 'Grün look.cube'
        shutil.copyfile(Path(__file__).resolve().parent / 'fixtures' / 'half-green.cube', cube)
        pixels = {}
        for name, profile, look in (('linear', linear, 'NONE'), ('quadratic', nonlinear, 'NONE'),
                                    ('look', linear, 'LUT')):
            target = folder / (name + '.exr')
            raw_backend.decode(raw_path, target, {'camera_input': 'ICC', 'profile_path': str(profile),
                                                  'look': look, 'lut_path': str(cube), 'lut_space': 'SRGB',
                                                  'preview': True, 'white_balance': 'CAMERA'})
            pixels[name] = oiio.ImageBuf(str(target)).get_pixels(oiio.FLOAT)
            assert np.isfinite(pixels[name]).all()
        assert np.mean(np.abs(pixels['linear'] - pixels['quadratic'])) > .002
        assert np.mean(np.abs(pixels['linear'] - pixels['look'])) > .0005
        print('User camera ICC and independent sRGB 3D LUT change real RAW preview pixels', flush=True)
        gallery_roundtrip(folder, raw_path, linear, nonlinear, cube, pixels)


with tempfile.TemporaryDirectory(prefix='Photo Editor ICC ') as temp:
    args = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else []
    run(Path(temp), args[0] if args else None)
