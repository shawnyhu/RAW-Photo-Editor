"""Check crop guides with Python/NumPy or with the installed extension in Blender."""
from importlib.util import find_spec
from pathlib import Path
import sys

import numpy as np

if find_spec('bpy') is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'blender_raw'))
    import crop_geometry as geom
else:
    from bl_ext.user_default.raw_photo_editor import crop_geometry as geom


def assert_inside(frame):
    # A crop cannot cross the horizon between corners. Exercise the float32
    # coefficients used by the compositor as well as the double-precision fit.
    uv = geom.project(frame.inverse.astype(np.float32), geom.UNIT)
    assert np.all(uv >= -2e-5) and np.all(uv <= 1 + 2e-5), uv
    assert 0 < np.prod(frame.fractions) <= 1 + 1e-8, frame.fractions


def check_previous_recipe():
    points = ((.10, .15), (.90, .20), (.77, .85), (.26, .82))
    frame = geom.frame(1.5, 1.25, points, .16, .72, (.64, .32), legacy_bounds=True)
    np.testing.assert_allclose(frame.fractions, (.58902245, .70682694), atol=1e-7)
    sample = geom.project(frame.inverse, ((0, 0), (.5, .5), (1, 1)))
    np.testing.assert_allclose(sample, ((.20214267, .18238386), (.58710834, .53489082),
                                        (.85183453, .77729641)), atol=1e-7)


def check_independent_axes():
    points = np.array(((.14, .09), (.84, .22), (.69, .84), (.24, .75)))
    for axis, coordinate, pairs in [('VERTICAL', 0, ((0, 3), (1, 2))),
                                    ('HORIZONTAL', 1, ((0, 1), (3, 2)))]:
        matrix, _ = geom.transform(1.5, points, 0., axis)
        result = geom.project(matrix, points)
        for a, b in pairs:
            assert abs(result[a, coordinate] - result[b, coordinate]) < 1e-10
        # Existing lines perpendicular to the correction axis keep their direction.
        a, b = ([(.1, .4), (.9, .4)] if axis == 'VERTICAL' else [(.4, .1), (.4, .9)])
        corrected = geom.project(matrix, (a, b))
        assert abs(corrected[0, 1-coordinate] - corrected[1, 1-coordinate]) < 1e-10
        # The other two measured edges retain a residual slope on this chart.
        other = (1, 0) if axis == 'VERTICAL' else (3, 0)
        assert abs(result[other[0], 1-coordinate] - result[other[1], 1-coordinate]) > .01
        assert_inside(geom.frame(1.5, .8, points, .31, .72, (.15, .9), axis))

    # The photographed lines determine the transform. Selecting a longer or
    # shorter part of those lines must give the same crop, including when the
    # lines converge inside the photo and the image center is behind the horizon.
    for left, right in [((.23, .15), (-.14, .90)), ((.25, .20), (.45, .08))]:
        def guides(heights, horizontal):
            uv = np.array([(m*y+b, y) for (m, b), y in zip((left, right, right, left), heights)])
            return uv[[0, 3, 2, 1], ::-1] if horizontal else uv

        for axis in ('VERTICAL', 'HORIZONTAL'):
            first = guides((.65, .68, .98, .91), axis == 'HORIZONTAL')
            second = guides((.77, .73, .89, .85), axis == 'HORIZONTAL')
            a = geom.frame(1.5, 1.2, first, .08, .83, (.64, .42), axis)
            b = geom.frame(1.5, 1.2, second, .08, .83, (.64, .42), axis)
            np.testing.assert_allclose(a.fractions, b.fractions, atol=1e-8)
            np.testing.assert_allclose(geom.project(a.inverse, geom.UNIT),
                                       geom.project(b.inverse, geom.UNIT), atol=1e-8)
            assert_inside(a)


def check_drag():
    initial = np.reshape(geom.GUIDES, (4, 2))
    target = np.array((.4, .72))
    previous = None
    for t in np.linspace(0., 1., 201):
        cursor = initial[2] * (1-t) + target * t
        quad = np.reshape(geom.drag_guides(initial, 2, cursor, 1.5), (4, 2))
        np.testing.assert_allclose(quad[2], cursor, atol=1e-7)
        np.testing.assert_array_equal(quad[[0, 1, 3]], initial[[0, 1, 3]])
        frame = geom.frame(1.5, 1.5, quad, 0., 1., (.5, .5))
        assert_inside(frame)
        crop = geom.project(frame.inverse, geom.UNIT)
        if previous is not None:
            assert np.max(abs(crop-previous)) < .025
        previous = crop

    crossing = geom.drag_guides(initial, 2, (.12, .10), 1.5)
    end = np.reshape(crossing, (4, 2))[2]
    # The handle follows the pointer to the diagonal, instead of sticking at its start.
    assert 1. < sum(end) < 1.0001 and end[0] < .52
    assert_inside(geom.frame(1.5, 1., crossing, 0., 1., (.5, .5)))
    released = geom.drag_guides(crossing, 2, (.78, .91), 1.5)
    np.testing.assert_allclose(released[4:6], (.78, .91), atol=1e-7)


if __name__ == '__main__':
    check_previous_recipe()
    check_independent_axes()
    check_drag()
    print('PASS legacy crop, independent axis corrections, continuous strong perspective and handle boundaries')
