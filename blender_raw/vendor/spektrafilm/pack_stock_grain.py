# SPDX-License-Identifier: GPL-3.0-or-later
"""Derive Lite's RGB particle areas from each added stock's characteristic curves.

The production grain shader divides each layer's fractional pixel area by its
particle area. This permits the same RMS-to-area inversion as Spektrafilm's
_coarsest_area_from_curves (andreavolpato/spektrafilm, commit 28bf883).
"""
import json
from pathlib import Path
import tarfile
import tomllib

import numpy as np


HERE = Path(__file__).resolve().parent


def calibration_for(profile, presets):
    info = profile["info"]
    grain = presets["defaults"]["color"][info["type"]] | presets.get(info["stock"], {})
    grain.update(info.get("custom_look", {}).get("grain", {}))
    if info["stock"] not in presets and not info.get("custom_look", {}).get("grain"):
        raise ValueError(f"Missing grain calibration for {info['stock']}")

    layers = np.asarray(profile["data"]["density_curves_layers"], dtype=float)
    maxima = np.nanmax(layers, axis=0)
    fractions = maxima / maxima.sum(axis=0)
    floor = fractions * grain["density_min"]
    density = np.nan_to_num(layers) + floor
    scales = np.asarray(grain["particle_scale_sublayers"], dtype=float)[:, None]
    variance = scales / fractions * density * (maxima + floor - grain["uniformity"] * density)
    peak = np.max(variance.sum(axis=1), axis=0)
    sigma = np.asarray(grain["rms_granularity"], dtype=float) / 1000
    areas = sigma**2 * (np.pi * 24**2) / peak  # 48 µm sensitometric aperture
    if not np.all(np.isfinite(areas) & (areas > 0)):
        raise ValueError(f"Invalid grain area for {info['stock']}")
    return areas.tolist()


def pack():
    with (HERE / "stock_grain.toml").open("rb") as file:
        presets = tomllib.load(file)
    areas = {}
    with tarfile.open(HERE / "additional-stock-data.tar.gz") as archive:
        films = json.load(archive.extractfile("Resources/data/stock_catalog.json"))["films"]
        for film in films:
            profile = json.load(archive.extractfile(f"Resources/data/profiles/{film}.json"))
            areas[film] = calibration_for(profile, presets)
    (HERE / "stock_grain_areas.json").write_text(
        json.dumps(areas, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


if __name__ == "__main__":
    pack()
