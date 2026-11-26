# SPDX-License-Identifier: GPL-3.0-or-later
"""Estimate perspective from sustained, spatially separated edges in input-image UV."""
from itertools import combinations
from typing import NamedTuple

import numpy as np

from . import crop_geometry as geom


class Line(NamedTuple):
    slope: float
    intercept: float        # Coordinate at the midpoint of the other axis.
    strength: float
    first: int
    last: int


def _smooth(image):
    padded = np.pad(image, ((0, 0), (1, 1)), mode='edge')
    image = (padded[:, :-2] + 2 * image + padded[:, 2:]) * .25
    padded = np.pad(image, ((1, 1), (0, 0)), mode='edge')
    return (padded[:-2] + 2 * image + padded[2:]) * .25


def _fit_line(x, y, normal_gradient, weights, slope, intercept, height):
    center = (height - 1) / 2
    t = y - center
    distance = np.abs(x - slope*t - intercept)
    hit = distance <= 2
    if hit.sum() < max(12, .28 * height):
        return None
    # Keep one contrast polarity. A slanted band through an otherwise straight
    # rail can hit opposite edges at opposite ends and invent a camera tilt.
    positive = normal_gradient > 0
    keep = positive if weights[hit & positive].sum() >= weights[hit & ~positive].sum() else ~positive
    x, y, weights, t, hit = x[keep], y[keep], weights[keep], t[keep], hit[keep]
    if hit.sum() < max(10, .22 * height):
        return None
    # The Hough angle grid only proposes peaks. Fit to the actual edge positions.
    fit_y, fit_x, fit_w = t[hit], x[hit], weights[hit]
    mean_y, mean_x = np.average(fit_y, weights=fit_w), np.average(fit_x, weights=fit_w)
    variance = np.sum(fit_w * (fit_y - mean_y)**2)
    if variance < 1e-6:
        return None
    slope = np.sum(fit_w * (fit_y - mean_y) * (fit_x - mean_x)) / variance
    intercept = mean_x - slope*mean_y
    distance = np.abs(x - slope*t - intercept)
    hit = distance <= 2
    rows = np.unique(y[hit])
    if len(rows) < .34 * height:
        return None
    # Thin edges must persist along the image. A cluster of points or a few
    # isolated high-contrast objects cannot stand in for an architectural line.
    coverage = np.bincount(rows*20//height, minlength=20) >= max(1, .53 * height / 20)
    if coverage.sum() < 7 or not np.any(np.convolve(coverage.astype(int), np.ones(4), 'valid') == 4):
        return None
    core = weights[hit].sum()
    flanks = weights[(distance >= 4) & (distance < 8)].sum()
    if core < .9 * flanks:  # Compare a four-pixel line band to two adjacent bands of equal width.
        return None
    return Line(float(slope), float(intercept), float(core * coverage.mean()), int(rows[0]), int(rows[-1]))


def _line_pair(x, y, gx, gy, weights, width, height):
    """Find x = slope * (y - mid_y) + intercept. Transpose for the horizontal family."""
    center = (height - 1) / 2
    offset = width + height
    candidates = []
    for degrees in range(-32, 33, 2):
        slope = np.tan(np.radians(degrees))
        aligned = np.abs(gy + slope*gx) < .25 * np.abs(gx)
        if aligned.sum() < max(24, height * .55):
            continue
        xx, yy, gradients, ww = x[aligned], y[aligned], gx[aligned], weights[aligned]
        bins = np.rint(xx - slope*(yy - center)).astype(int) + offset
        votes = np.convolve(np.bincount(bins, weights=ww, minlength=3*offset+1), (1, 2, 1), 'same')
        for _ in range(7):
            peak = int(np.argmax(votes))
            if votes[peak] == 0:
                break
            votes[max(0, peak-6):peak+7] = 0
            line = _fit_line(xx, yy, gradients, ww, slope, peak-offset, height)
            if line is not None:
                candidates.append(line)

    lines = []
    for line in sorted(candidates, key=lambda item: item.strength, reverse=True):
        if all(abs(line.intercept - other.intercept) > max(5, width*.035) for other in lines):
            lines.append(line)
            if len(lines) == 18:
                break
    pairs = []
    total = sum(line.strength for line in lines)
    for first, second in combinations(lines, 2):
        left, right = sorted((first, second), key=lambda item: item.intercept)
        gap = right.intercept - left.intercept
        if gap < .23*width or min(left.last, right.last) - max(left.first, right.first) < .24*height:
            continue
        gradient = (right.slope - left.slope) / gap
        mid_slope = left.slope + gradient*((width-1)/2 - left.intercept)
        if abs(mid_slope) > .30 or abs(gradient)*width > 1.1:
            continue
        # Lines belonging to a common vanishing point have slopes that vary
        # linearly with their intercept. Conflicting facades make a poor global fit.
        agreement = sum(line.strength for line in lines
                        if abs(line.slope - left.slope - gradient*(line.intercept-left.intercept)) < .045) / total
        if agreement >= .58:
            score = np.sqrt(left.strength*right.strength) * min(1., gap/(.55*width)) * agreement
            pairs.append((score, left, right, mid_slope, gradient*width))
    if not pairs:
        return None
    pairs.sort(key=lambda pair: pair[0], reverse=True)
    score, left, right, mid, tilt = pairs[0]
    for other_score, _, _, other_mid, other_tilt in pairs[1:]:
        if other_score < .88*score:
            break
        if abs(mid - other_mid) + .5 * abs(tilt - other_tilt) > .085:
            return None

    # Sub-degree differences between nearly aligned edges are often sampling noise.
    if max(abs(left.slope), abs(right.slope)) < .012:
        return ((0., left.intercept), (0., right.intercept))
    return ((left.slope, left.intercept), (right.slope, right.intercept))


def _neutral_pair(length):
    return ((0., .2*length-.5), (0., .8*length-.5))


def _guides(vertical, horizontal, width, height, direction):
    points = []
    for col, row in ((0, 0), (1, 0), (1, 1), (0, 1)):
        a, x0 = vertical[col]
        b, y0 = horizontal[row]
        xx = (x0 - a*(height-1)/2 + a*(y0 - b*(width-1)/2)) / (1-a*b)
        yy = b*(xx-(width-1)/2) + y0
        points.extend(((xx+.5)/width, (yy+.5)/height))
    if min(points) < 0 or max(points) > 1:
        raise ValueError('Detected guides extend beyond the image. Use manual guides')
    aspect = width/height
    fitted = geom.frame(aspect, aspect, points, 0., 1., (.5, .5), axis=direction)
    # A bounded editing canvas can hide a vanishing line or excessive stretch
    # near the edge of the original. Homographic area magnification varies as
    # 1/w^3, and its linear denominator has its extrema at source corners.
    warp = fitted.source_to_work
    full_depth = geom.UNIT @ warp[2, :2] + warp[2, 2]
    crosses_horizon = full_depth.min() <= 0 <= full_depth.max()
    magnitudes = abs(full_depth)
    excessive_stretch = magnitudes.max() > np.cbrt(10.) * magnitudes.min()
    source = geom.project(fitted.inverse, geom.UNIT)
    area = abs(geom.cross(source[2]-source[0], source[3]-source[1])) * .5
    depth = geom.UNIT @ fitted.inverse[2, :2] + fitted.inverse[2, 2]
    if crosses_horizon or excessive_stretch or area < .48 or max(abs(depth)) / min(abs(depth)) > 1.95:
        raise ValueError('Automatic correction would stretch or discard too much of the image. Use manual guides')
    return points


def detect(gray, direction='BOTH'):
    """Return BL, BR, TR, TL guide UVs. Raise ValueError if any requested axis lacks evidence."""
    if direction not in {'VERTICAL', 'HORIZONTAL', 'BOTH'}:
        raise ValueError('Choose Vertical, Horizontal or Both')
    gray = np.asarray(gray, dtype=np.float32)
    if gray.ndim != 2 or min(gray.shape) < 28 or not np.isfinite(gray).all():
        raise ValueError('A finite luminance image at least 28 pixels on each side is required')
    height, width = gray.shape
    gy, gx = np.gradient(_smooth(np.log1p(np.maximum(gray, 0)*8)))
    mag = np.hypot(gx, gy)
    mag[:2] = mag[-2:] = 0
    mag[:, :2] = mag[:, -2:] = 0
    y, x = np.nonzero(mag >= max(.005, np.percentile(mag, 88)))
    if len(x) < 45:
        raise ValueError('Too few visible straight edges. Use manual guides')
    weights = np.minimum(mag[y, x], np.percentile(mag[y, x], 85))
    gx, gy = gx[y, x], gy[y, x]
    vertical = _neutral_pair(width) if direction == 'HORIZONTAL' else _line_pair(x,y,gx,gy,weights,width,height)
    horizontal = _neutral_pair(height) if direction == 'VERTICAL' else _line_pair(y,x,gy,gx,weights,height,width)
    if vertical is None and horizontal is None:
        raise ValueError('No reliable horizontal or vertical lines. Use manual guides')
    if vertical is None or horizontal is None:
        missing = 'vertical' if vertical is None else 'horizontal'
        other = 'Horizontal' if vertical is None else 'Vertical'
        if direction == 'BOTH':
            try:
                _guides(vertical or _neutral_pair(width), horizontal or _neutral_pair(height),
                        width, height, other.upper())
            except ValueError:
                raise ValueError(f'No reliable {missing} line pair and no safe {other.lower()} correction. '
                                 'Use manual guides') from None
            raise ValueError(f'No reliable {missing} line pair. Try {other}')
        raise ValueError(f'No reliable {missing} line pair. Use manual guides')
    return _guides(vertical, horizontal, width, height, direction)
