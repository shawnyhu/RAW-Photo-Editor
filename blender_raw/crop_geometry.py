# SPDX-License-Identifier: GPL-3.0-or-later
"""Crop geometry. UV origin is bottom left. Working units are image heights."""
from itertools import combinations
from math import cos, sin, sqrt
from types import SimpleNamespace

import numpy as np

UNIT = np.array(((0., 0.), (1., 0.), (1., 1.), (0., 1.)))
GUIDES = (.15, .15, .85, .15, .85, .85, .15, .85)
# Small enough for a subpixel adjustment; also survives round-tripping RNA float vectors.
_GUIDE_EPS = 1e-5
PRESETS = {'SQUARE': 1., 'FOUR_THREE': 4/3, 'THREE_TWO': 1.5,
           'FIVE_FOUR': 1.25, 'WIDE': 16/9, 'CINEMA': 2.39}


def cross(a, b):
    return a[..., 0] * b[..., 1] - a[..., 1] * b[..., 0]


def project(matrix, points):
    pts = np.atleast_2d(points)
    h = pts @ matrix[:, :2].T + matrix[:, 2]
    if np.any(abs(h[:, 2]) < 1e-7) or np.min(h[:, 2]) * np.max(h[:, 2]) < 0:
        raise ValueError('The perspective horizon crosses the photograph')
    return h[:, :2] / h[:, 2, None]


def homography(src, dst):
    rows = []
    for (x, y), (u, v) in zip(src, dst):
        rows.extend(((x, y, 1, 0, 0, 0, -u*x, -u*y),
                     (0, 0, 0, x, y, 1, -v*x, -v*y)))
    try:
        return np.append(np.linalg.solve(rows, dst.ravel()), 1).reshape(3, 3)
    except np.linalg.LinAlgError as exc:
        raise ValueError('Separate the four guide corners') from exc


def _four_guides(aspect, quad, legacy_bounds):
    edges = np.roll(quad, -1, axis=0) - quad
    lengths = np.linalg.norm(edges, axis=1)
    turn = cross(edges, np.roll(edges, -1, axis=0))
    invalid = (min(turn) < .002 if legacy_bounds else
               np.any(turn <= _GUIDE_EPS * np.maximum(lengths, np.roll(lengths, -1))))
    if invalid:
        raise ValueError('Keep the guide corners in order and apart')
    lengths = np.linalg.norm(edges * (aspect, 1), axis=1)
    target = UNIT * (.5*(lengths[0]+lengths[2]), .5*(lengths[1]+lengths[3]))
    if legacy_bounds:
        return homography(quad, target)
    # A valid set of guides may put the origin on the projective horizon.
    # Normalize at an interior point of the guides, where the map is finite.
    center = np.mean(quad, axis=0)
    warp = homography(quad-center, target)
    warp[:, 2] -= warp[:, :2] @ center
    return warp


def _axis_guides(aspect, quad, axis):
    # In (u,v), the two references are u = left(v) and u = right(v).
    # Dividing by their linear separation makes each line vertical, while
    # constant-v rows stay horizontal. Swap coordinates for a horizontal pair.
    horizontal = axis == 'HORIZONTAL'
    points = quad[[0, 3, 2, 1], ::-1] if horizontal else quad
    edges = points[[3, 2]] - points[[0, 1]]
    if np.any(abs(edges[:, 1]) <= _GUIDE_EPS):
        raise ValueError('Give each guide two separated endpoints along its axis')
    slopes = edges[:, 0] / edges[:, 1]
    intercepts = points[:2, 0] - slopes * points[:2, 1]
    width_slope = slopes[1] - slopes[0]
    width_zero = intercepts[1] - intercepts[0]
    # The guides describe infinite lines. Sliding a handle along its own edge
    # must not change the correction or its scale. Anchor at the image midline,
    # or at the midpoint of the visible front half if the vanishing line cuts it.
    width_one = width_zero + width_slope
    if max(width_zero, width_one) <= _GUIDE_EPS:
        raise ValueError('Keep the two guides apart on the photograph')
    middle = .5
    if min(width_zero, width_one) < 0:
        crossing = -width_zero / width_slope
        middle = (crossing + (1. if width_one > 0 else 0.)) * .5
    width = width_slope * middle + width_zero
    matrix = np.array(((1., -slopes[0], -intercepts[0]),
                       (0., 1., -middle), (0., width_slope / width, width_zero / width)))
    if horizontal:
        matrix = matrix[[1, 0, 2]][:, [1, 0, 2]]
    matrix[0] *= aspect
    anchor = (middle, .5) if horizontal else (.5, middle)
    return matrix, np.array(anchor)


def _clip_half_plane(polygon, line):
    distance = polygon @ line[:2] + line[2]
    result = []
    for i, end in enumerate(polygon):
        before, after = distance[i-1], distance[i]
        if (before < 0) != (after < 0):
            start = polygon[i-1]
            result.append(start + (end-start) * (before / (before-after)))
        if after >= 0:
            result.append(end)
    return np.asarray(result, dtype=float).reshape(-1, 2)


def _finite_image(warp, aspect, middle, center):
    # When a vanishing line cuts the original image its corrected footprint is
    # unbounded. Intersect the front-facing source with a finite working canvas
    # around the measured guides. Applying this same envelope on both sides of
    # the horizon transition keeps a drag continuous and avoids extreme zoom.
    # Keep the reference in roughly its source position on a canvas with 15%
    # overscan per side. A near-horizon sliver must not dominate the fitted frame.
    dimensions = np.array((aspect, 1.))
    lo = center - (middle + .15) * dimensions
    hi = lo + 1.3 * dimensions
    divisors = UNIT @ warp[2, :2] + warp[2, 2]
    if np.all(divisors > 1e-7):
        image = project(warp, UNIT)
        if np.all((image >= lo) & (image <= hi)):
            return image
    polygon = lo + UNIT * (hi-lo)
    u, v, w = np.linalg.inv(warp)
    # u/w and v/w must both lie in [0,1] with w positive.
    for line in (u, w-u, v, w-v):
        polygon = _clip_half_plane(polygon, line)
    if len(polygon):
        edges = polygon - np.roll(polygon, 1, axis=0)
        polygon = polygon[np.linalg.norm(edges, axis=1) > 1e-11 * max(1., aspect)]
    if len(polygon) < 3:
        raise ValueError('No usable crop fits these guides')
    return polygon


def transform(aspect, guides, angle, axis='BOTH', *, legacy_bounds=False):
    quad = np.asarray(guides, dtype=float).reshape(4, 2)
    if not np.isfinite(quad).all() or not np.isfinite(aspect) or aspect <= 0:
        raise ValueError('Invalid photograph aspect or guide coordinates')
    if axis == 'BOTH':
        warp = _four_guides(aspect, quad, legacy_bounds)
    elif axis in {'HORIZONTAL', 'VERTICAL'}:
        warp, middle = _axis_guides(aspect, quad, axis)
    else:
        raise ValueError('Unknown perspective axis')
    rotate = np.array(((cos(angle), -sin(angle), 0), (sin(angle), cos(angle), 0), (0, 0, 1)))
    matrix = rotate @ warp
    if axis == 'BOTH' and legacy_bounds:
        # Preserve the exact frame and coefficients stored by existing four-guide recipes.
        image = project(matrix, UNIT)
        if np.abs(image).max() > 50 * max(1, aspect):
            raise ValueError('Perspective is too steep. Move the guides closer to parallel')
        return matrix, image
    if axis == 'BOTH':
        middle = quad.mean(axis=0)
        guided = project(warp, quad)
        center = (guided.min(axis=0) + guided.max(axis=0)) * .5
    else:
        center = project(warp, middle)[0]
    image = _finite_image(warp, aspect, middle, center)
    return matrix, image @ rotate[:2, :2].T


def drag_guides(guides, index, uv, aspect, axis='BOTH'):
    """Move a reference endpoint to the cursor, sliding up to a convexity boundary."""
    quad = np.asarray(guides, dtype=float).reshape(4, 2).copy()
    start = quad[index].copy()
    end = np.clip(uv, 0., 1.)

    def valid(t):
        quad[index] = np.asarray(start + (end-start)*t, dtype=np.float32)
        try:
            transform(aspect, quad, 0., axis)
        except ValueError:
            return False
        return True

    if not valid(1.):
        lo, hi = 0., 1.
        for _ in range(20):
            t = (lo+hi)*.5
            if valid(t):
                lo = t
            else:
                hi = t
        quad[index] = np.asarray(start + (end-start)*lo, dtype=np.float32)
    return tuple(quad.ravel())


def frame(aspect, ratio, guides, angle, size, position, axis='BOTH', *, legacy_bounds=False):
    matrix, polygon = transform(aspect, guides, angle, axis, legacy_bounds=legacy_bounds)
    # Each edge constrains the center and half-height of an inscribed rectangle.
    edges = np.roll(polygon, -1, axis=0) - polygon
    normals = np.column_stack((edges[:, 1], -edges[:, 0]))
    normals /= np.linalg.norm(normals, axis=1)[:, None]
    b = np.sum(normals * polygon, axis=1)
    margin = abs(normals[:, 0]) * ratio + abs(normals[:, 1])
    planes = np.column_stack((normals, margin))
    solutions = []
    for ix in combinations(range(len(polygon)), 3):
        indices = list(ix)
        try:
            point = np.linalg.solve(planes[indices], b[indices])
        except np.linalg.LinAlgError:
            continue
        if point[2] > 0 and np.all(planes @ point <= b + 1e-8):
            solutions.append(point)
    if not solutions:
        raise ValueError('No usable crop fits these guides')
    best = max(solutions, key=lambda p: p[2])
    half = best[2] * min(1., max(.01, size))
    lo, hi = polygon.min(axis=0), polygon.max(axis=0)
    wanted = lo + np.asarray(position) * (hi - lo)
    limits = b - half * margin
    # Nearest feasible center preserves a dragged frame's size at the image edge.
    candidates = [best[:2]]
    for n, bound in zip(normals, limits):
        candidates.append(wanted - n * (n @ wanted - bound))
    candidates.append(wanted)
    for ix in combinations(range(len(polygon)), 2):
        indices = list(ix)
        if abs(np.linalg.det(normals[indices])) > 1e-8:
            candidates.append(np.linalg.solve(normals[indices], limits[indices]))
    center = min((p for p in candidates if np.all(normals @ p <= limits + 1e-8)),
                 key=lambda p: np.linalg.norm(p - wanted))
    extent = 2 * half * np.array((ratio, 1.))
    lower = center - extent / 2
    box = np.array(((extent[0], 0, lower[0]), (0, extent[1], lower[1]), (0, 0, 1)))
    inverse = np.linalg.solve(matrix, box)
    # Limit output sample count to the input's pixel budget under extreme keystone.
    density = min(1., sqrt(aspect / np.prod(extent)))
    fractions = density * extent / (aspect, 1.)
    return SimpleNamespace(inverse=inverse, source_to_work=matrix, lower=lower, center=center,
                           extent=extent, max_height=2*best[2], lo=lo, hi=hi, fractions=fractions)


def ratio_for(preset, aspect, free, flip):
    base = PRESETS.get(preset, free if preset == 'FREE' and free > 0 else aspect)
    return 1/base if flip else base
