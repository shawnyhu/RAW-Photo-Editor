# SPDX-License-Identifier: GPL-3.0-or-later
"""Crop geometry. UV origin is bottom left. Working units are image heights."""
from itertools import combinations
from math import cos, sin, sqrt
from types import SimpleNamespace

import numpy as np

UNIT = np.array(((0., 0.), (1., 0.), (1., 1.), (0., 1.)))
GUIDES = (.15, .15, .85, .15, .85, .85, .15, .85)
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


def transform(aspect, guides, angle):
    quad = np.reshape(guides, (4, 2))
    edges = np.roll(quad, -1, axis=0) - quad
    if min(cross(edges, np.roll(edges, -1, axis=0))) < .002:
        raise ValueError('Keep the guide corners in order and apart')
    lengths = np.linalg.norm(edges * (aspect, 1), axis=1)
    target = UNIT * (.5*(lengths[0]+lengths[2]), .5*(lengths[1]+lengths[3]))
    warp = homography(quad, target)
    rotate = np.array(((cos(angle), -sin(angle), 0), (sin(angle), cos(angle), 0), (0, 0, 1)))
    matrix = rotate @ warp
    bounds = project(matrix, UNIT)
    if np.abs(bounds).max() > 50 * max(1, aspect):
        raise ValueError('Perspective is too steep. Move the guides closer to parallel')
    return matrix, bounds


def frame(aspect, ratio, guides, angle, size, position):
    matrix, polygon = transform(aspect, guides, angle)
    # Each edge constrains the center and half-height of an inscribed rectangle.
    edges = np.roll(polygon, -1, axis=0) - polygon
    normals = np.column_stack((edges[:, 1], -edges[:, 0]))
    normals /= np.linalg.norm(normals, axis=1)[:, None]
    b = np.sum(normals * polygon, axis=1)
    margin = abs(normals[:, 0]) * ratio + abs(normals[:, 1])
    planes = np.column_stack((normals, margin))
    solutions = []
    for ix in combinations(range(4), 3):
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
    for ix in combinations(range(4), 2):
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
