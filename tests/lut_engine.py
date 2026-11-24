"""Verify the installed .cube reader, domains, interpolation and scene-linear output."""
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from bl_ext.user_default.raw_photo_editor import lut


def cube(mapping, header=""):
    # .cube files enumerate the red index fastest.
    rows = [" ".join(map(str, mapping(r, g, b)))
            for b in (0, 1) for g in (0, 1) for r in (0, 1)]
    return ("LUT_3D_SIZE 2\n" + header + "\n".join(rows) + "\n").encode()


def reject(path, **kwargs):
    try:
        lut.load(path, **kwargs)
    except ValueError as error:
        assert str(error), path
    else:
        raise AssertionError(f"Accepted invalid LUT: {path}")


with TemporaryDirectory(prefix="LUT test ") as temp:
    path = Path(temp) / "Fotografía 三维.CUBE"
    samples = np.array([[[0, 0, 0], [1, 1, 1], [.18, .4, .7],
                         [-.08, .3, .73], [.23, 1.4, 8], [-.5, -.3, -.1]]], np.float32)
    identity = cube(lambda r, g, b: (r, g, b))
    path.write_bytes(b'\xef\xbb\xbf# test\r\nTITLE "Identity"\r\n' + identity)
    for space in ("LINEAR", "SRGB"):
        result = lut.load(path, space)(samples)
        assert result.dtype == np.float32
        np.testing.assert_allclose(result, samples, rtol=2e-6, atol=3e-6)

    # Cover replacing a linked LUT at the same filename during a Blender session.
    old_hash = sha256(path.read_bytes()).hexdigest()
    swap = cube(lambda r, g, b: (b, g, r)).replace(b"LUT_3D_SIZE", b"lut_3d_size")
    path.write_bytes(swap)
    reject(path, expected_hash=old_hash)
    for space in ("LINEAR", "SRGB"):
        result = lut.load(path, space)(samples)
        np.testing.assert_allclose(result, samples[..., ::-1], rtol=3e-6, atol=3e-6)

    # A red/green/blue corner test that differentiates tetrahedral interpolation
    # (minimum channel) from trilinear interpolation (product of the channels).
    path.write_bytes(cube(lambda r, g, b: (min(r, g, b),) * 3))
    np.testing.assert_allclose(lut.load(path, "LINEAR")(samples[:, 2:3]), .18, atol=3e-6)

    path.write_bytes(cube(lambda r, g, b: (.5 * r, .5 * g, .5 * b)))
    mid = np.full((1, 1, 3), .18, np.float32)
    np.testing.assert_allclose(lut.load(path, "LINEAR")(mid), .09, atol=2e-6)
    encoded = 1.055 * .18 ** (1 / 2.4) - .055
    expected = ((.5 * encoded + .055) / 1.055) ** 2.4
    np.testing.assert_allclose(lut.load(path)(mid), expected, atol=2e-6)

    domain = "DOMAIN_MIN -1 -2 -3\nDOMAIN_MAX 1 2 3\n"
    path.write_bytes(cube(lambda r, g, b: (b, g, r), domain))
    np.testing.assert_allclose(lut.load(path, "LINEAR")(np.array([-.5, 0, 1.5], np.float32)),
                               [.75, .5, .25], atol=3e-6)
    # Resolve combined .cube: the shaper input [-1,2] and the 3D table
    # input [-.5,.5] must both be honored, and in that order.
    path.write_bytes(b"LUT_1D_SIZE 3\nLUT_1D_INPUT_RANGE -1 2\n" +
                     cube(lambda r, g, b: (b, g, r), "LUT_3D_INPUT_RANGE -.5 .5\n"
                          "-.5 -.5 -.5\n0 0 0\n.5 .5 .5\n"))
    np.testing.assert_allclose(lut.load(path, "LINEAR")(np.array([.5, -.25, 1.25], np.float32)),
                               [.75, .25, .5], atol=3e-6)

    for contents in (b"not a cube file", b"LUT_3D_SIZE 1\n0 0 0\n", b"LUT_3D_SIZE 130\n",
                     b"LUT_1D_SIZE 2\n0 0 0\n1 1 1\n",
                     identity.rsplit(b"1 1 1", 1)[0], identity.replace(b"0 0 0", b"nan 0 0"),
                     cube(lambda r, g, b: (r, g, b), "DOMAIN_MAX 1 0 1\n")):
        path.write_bytes(contents)
        reject(path)
    reject(Path(temp) / "missing.cube")
    reject(Path(temp) / "wrong.icc")
    reject(path, space="UNKNOWN")
    print("3D LUT: tetrahedral, per-channel domains, Resolve shaper, sRGB and linear,"
          " extended range, same-path replacement and invalid files passed", flush=True)
