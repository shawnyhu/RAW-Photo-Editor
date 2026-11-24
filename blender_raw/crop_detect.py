# SPDX-License-Identifier: GPL-3.0-or-later
"""Dominant straight guides on an oriented, downsampled luminance image."""
from itertools import combinations
import numpy as np

from .crop_geometry import transform


def line_family(x, y, gx, gy, weights, width, height):
    """Fit x = slope * (y - mid_y) + intercept; transpose for horizontal lines."""
    minimum = max(14, height * .13)
    rho_max = width + height
    center = (height - 1) / 2
    candidates = []
    for angle in range(-32, 33, 2):
        slope = np.tan(np.radians(angle))
        compatible = abs(gy + slope*gx) < .36*(abs(gx)+1e-8)
        if np.count_nonzero(compatible) < minimum:
            continue
        xx, yy, ww = x[compatible], y[compatible], weights[compatible]
        intercept = xx - slope*(yy - center)
        bins = np.rint(intercept).astype(int) + rho_max
        hist = np.bincount(bins, weights=ww, minlength=3*rho_max+1)
        votes = np.convolve(hist, (1, 2, 1), mode='same')
        for _ in range(5):
            peak = int(np.argmax(votes))
            strength = votes[peak]
            if strength <= 0:
                break
            votes[max(0, peak-7):peak+8] = 0
            hit = abs(intercept - (peak-rho_max)) <= 1.75
            if np.count_nonzero(hit) < minimum:
                continue
            ys = yy[hit]
            occupied = np.unique((ys * 20 / height).astype(int)).size
            if occupied < 7:
                continue
            fit = np.polyfit(ys-center, xx[hit], 1, w=np.sqrt(ww[hit]))
            candidates.append((float(strength * occupied), *fit))
    candidates.sort(reverse=True)
    lines = []
    for line in candidates:
        if all(abs(line[2]-other[2]) > width*.042 or abs(line[1]-other[1]) > .14 for other in lines):
            lines.append(line)
            if len(lines) == 18:
                break
    pairs = []
    for a, b in combinations(lines, 2):
        left, right = sorted((a, b), key=lambda q: q[2])
        gap = right[2] - left[2]
        if gap < .20*width or abs(left[1]-right[1]) > .75:
            continue
        score = np.sqrt(a[0]*b[0]) * min(1., gap/(.55*width))
        pairs.append((score, left, right))
    return sorted(pairs, reverse=True)[:12]


def detect(gray):
    gray = np.asarray(gray, dtype=np.float32)
    height, width = gray.shape
    if min(height, width) < 28:
        raise ValueError('The reference image is too small')
    gy, gx = np.gradient(np.log1p(np.maximum(gray, 0)*8))
    mag = np.hypot(gx, gy)
    mag[:2] = mag[-2:] = 0
    mag[:, :2] = mag[:, -2:] = 0
    y, x = np.nonzero(mag > max(.003, np.percentile(mag, 88)))
    if len(x) < 70:
        raise ValueError('Too few visible straight edges. Use Four Guides')
    weights = np.minimum(mag[y, x], np.percentile(mag[y, x], 85))
    vertical = line_family(x,y,gx[y,x],gy[y,x],weights,width,height)
    horizontal = line_family(y,x,gy[y,x],gx[y,x],weights,height,width)
    for _, v, h in sorted(((a[0]*b[0], a, b) for a in vertical for b in horizontal),
                          key=lambda pair: pair[0], reverse=True):
        points = []
        for col, row in ((v[1], h[1]), (v[2], h[1]), (v[2], h[2]), (v[1], h[2])):
            a, b = col[1], row[1]
            xx = (col[2]-a*(height-1)/2 + a*(row[2]-b*(width-1)/2))/(1-a*b)
            yy = b*(xx-(width-1)/2) + row[2]
            points.extend(((xx+.5)/width, (yy+.5)/height))
        if min(points) < 0 or max(points) > 1:
            continue
        try:
            transform(width/height, points, 0.)
            return points
        except ValueError:
            continue
    raise ValueError('No reliable horizontal and vertical pair. Use Four Guides')
