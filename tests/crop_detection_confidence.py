# SPDX-License-Identifier: GPL-3.0-or-later
"""Check automatic correction evidence without external photographs or GPU rendering.

blender -b --factory-startup --offline-mode --python-exit-code 1 -P tests/crop_detection_confidence.py
"""
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from blender_raw import crop_detect, crop_geometry as geom


def chart(segments, shape=(192, 256)):
    y, x = np.indices(shape)
    gray = np.full(shape, .1)
    for a, b in segments:
        a, b = np.asarray((a, b)) * shape[::-1]
        edge = b-a
        along = ((x-a[0])*edge[0] + (y-a[1])*edge[1]) / (edge @ edge)
        distance = ((x-a[0])*edge[1] - (y-a[1])*edge[0]) / np.linalg.norm(edge)
        gray += np.where((along >= 0) & (along <= 1), np.exp(-.5*distance**2), 0)
    return gray


def rejected(gray, direction, hint=None):
    try:
        crop_detect.detect(gray, direction)
    except ValueError as error:
        if hint:
            assert hint in str(error), error
    else:
        raise AssertionError(f'{direction}: weak or unsafe correction was accepted')


def check_axis(gray, direction, segments):
    quad = np.reshape(crop_detect.detect(gray, direction), (4, 2))
    aspect = gray.shape[1] / gray.shape[0]
    warp, _ = geom.transform(aspect, quad, 0., axis=direction)
    axis = 0 if direction == 'VERTICAL' else 1
    for segment in segments:
        endpoints = geom.project(warp, segment)
        assert abs(endpoints[0, axis] - endpoints[1, axis]) < .035, (direction, endpoints)
    # When asking for one family, already aligned lines of the other family
    # must remain aligned at several positions across the corrected frame.
    for t in (.1, .5, .9):
        other = ((0, t), (1, t)) if axis == 0 else ((t, 0), (t, 1))
        ends = geom.project(warp, other)
        assert abs(ends[0, 1-axis] - ends[1, 1-axis]) < 1e-8


corners = np.array(((.10, .15), (.90, .20), (.77, .85), (.26, .82)))
vertical = ((corners[0], corners[3]), (corners[1], corners[2]))
horizontal = ((corners[0], corners[1]), (corners[3], corners[2]))
np.testing.assert_allclose(np.reshape(crop_detect.detect(chart(vertical+horizontal)), (4, 2)), corners, atol=.035)
for direction, segments, other in [('VERTICAL', vertical, 'Horizontal'), ('HORIZONTAL', horizontal, 'Vertical')]:
    image = chart(segments)
    check_axis(image, direction, segments)
    rejected(image, other.upper())
    rejected(image, 'BOTH', 'Try ' + direction.title())
    rejected(chart([segments[0]]), direction)

rng = np.random.default_rng(1031)
for shape in ((96, 128), (256, 384)):
    noise = rng.uniform(0., 1., shape)
    for direction in ('VERTICAL', 'HORIZONTAL', 'BOTH'):
        rejected(noise, direction)
rejected(np.zeros((192, 256)), 'BOTH')
rejected(chart([((.45, .12), (.49, .86)), ((.51, .11), (.53, .85))]), 'VERTICAL')
rejected(chart([((.2, .40), (.2, .60)), ((.8, .40), (.8, .60))]), 'VERTICAL')

# With aligned architectural edges, Auto must leave the frame unchanged.
rect = np.array(((.14, .16), (.82, .16), (.82, .81), (.14, .81)))
aligned = crop_detect.detect(chart(zip(rect, np.roll(rect, -1, axis=0))))
fitted = geom.frame(4/3, 4/3, aligned, 0., 1., (.5, .5))
np.testing.assert_allclose(geom.project(fitted.inverse, geom.UNIT), geom.UNIT, atol=1e-8)

# These two long lines drive excessive stretching at the source image edge,
# even though the bounded working canvas can still produce an acceptable crop.
extreme = chart([((.10, .15), (.27, .85)), ((.90, .15), (.73, .85))])
rejected(extreme, 'VERTICAL', 'stretch or discard')
rejected(extreme, 'BOTH', 'no safe vertical correction')
rejected(extreme.T, 'HORIZONTAL', 'stretch or discard')
rejected(extreme.T, 'BOTH', 'no safe horizontal correction')

# The measured segments are distinct, but their vanishing point is inside
# the unmeasured upper part of this portrait. Clipping the workspace hides it.
crossing = chart([((.18, .02), (.37, .55)), ((.82, .02), (.63, .55))], shape=(384, 160))
rejected(crossing, 'VERTICAL', 'stretch or discard')
print('PASS automatic perspective: independent axes, confidence rejection, neutral aligned frame and conservative crop')
