# SPDX-License-Identifier: GPL-3.0-or-later
"""Derive Lite's grain areas and selectable-film recommendations from source data.

The production grain shader divides each layer's fractional pixel area by its
particle area. This permits the same RMS-to-area inversion as Spektrafilm's
_coarsest_area_from_curves (andreavolpato/spektrafilm, commit 28bf883).
"""
import ast
import json
from pathlib import Path
import tarfile
import tomllib

import numpy as np

from pack_stock_data import DYEFILMS, NEGS_REVISION, reference_time


HERE = Path(__file__).resolve().parent


def grain_settings(profile, presets):
    info = profile["info"]
    model = info["channel_model"]
    grain = presets["defaults"][model][info["type"]] | presets.get(info["stock"], {})
    grain.update(info.get("custom_look", {}).get("grain", {}))
    if info["stock"] not in presets and not info.get("custom_look", {}).get("grain"):
        # Negs grain_settings / upstream params_builder return the schema
        # defaults for a stock with no preset (currently legacy Verita 200D).
        grain = {"rms_granularity": [6.0, 8.0, 10.0], "density_min": [0.03] * 3,
                 "uniformity": [0.97] * 3, "particle_scale_sublayers": [1.0, 0.5, 0.25]}
    return grain


def calibration_for(profile, presets):
    info = profile["info"]
    grain = grain_settings(profile, presets)

    layers = np.asarray(profile["data"]["density_curves_layers"], dtype=float)
    if info["channel_model"] == "bw":
        index = profile["data"]["development_time"].index(reference_time(profile))
        layers = layers[:, :, index:index + 1]
    maxima = np.nanmax(layers, axis=0)
    fractions = maxima / maxima.sum(axis=0)
    floor = fractions * grain["density_min"]
    density = np.nan_to_num(layers) + floor
    scale_values = grain["particle_scale_sublayers"]
    # Negs sublayer_scales: a fourth curve component repeats the finest scale.
    scales = np.asarray([scale_values[min(i, len(scale_values) - 1)]
                         for i in range(layers.shape[1])], dtype=float)[:, None]
    variance = scales / fractions * density * (maxima + floor - grain["uniformity"] * density)
    peak = np.max(variance.sum(axis=1), axis=0)
    sigma = np.asarray(grain["rms_granularity"], dtype=float) / 1000
    areas = sigma**2 * (np.pi * 24**2) / peak  # 48 µm sensitometric aperture
    if not np.all(np.isfinite(areas) & (areas > 0)):
        raise ValueError(f"Invalid grain area for {info['stock']}")
    return areas.tolist() * (3 if info["channel_model"] == "bw" else 1)


def legacy_defaults(archive):
    """Read the matching legacy algorithms, without importing the generator."""
    source = archive.extractfile("tools/generate_profile_curves.py").read().decode()
    tree = ast.parse(source)
    wanted = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in {"_halation_preset", "_dir_coupler_defaults"}:
            wanted.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "HALATION_PRESETS"
                                                for t in node.targets):
            wanted.append(node)
    namespace = {}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), "legacy_profile_defaults", "exec"), namespace)
    return namespace


def rgb(values):
    return list(values) if len(values) == 3 else [values[0]] * 3


def recommended_defaults(profiles, catalog, presets, areas, legacy):
    films, papers = {}, {}
    matrix_keys = {"same_layer_rgb": "dirGammaSameLayerRgb", "r_to_gb": "dirGammaRToGb",
                   "g_to_rb": "dirGammaGToRb", "b_to_rg": "dirGammaBToRg"}
    for stock, profile in sorted(profiles.items()):
        info, meta = profile["info"], profile["metadata"]
        additional = stock in catalog["films"] or stock in catalog["papers"]
        source = ("The Dyeworks 2.2.3" if stock in DYEFILMS else
                  f"Negs {NEGS_REVISION}" if additional else "spektrafilm-ofx 86476afc5b077de77e2278e3658d1ba9309892a1")
        item = {"name": info["name"], "channel_model": info["channel_model"], "type": info["type"],
                "parameters": {}, "provenance": {"source": source, "profile_version": meta.get("version"),
                    "copyright": meta.get("copyright"), "license": meta.get("license"),
                    "datasource": meta.get("datasource")}}
        if info["channel_model"] == "bw":
            item["development_time_minutes"] = reference_time(profile)
            item["experimental"] = meta.get("version") == "bw-expansion-1"
        if info["stage"] == "printing":
            item["compatible_films"] = catalog["compatible_films"][stock]
            if stock == "ilford_mgrc_deluxe_grade2":
                item["limitations"] = ("Fixed grade 2, neutral B&W negatives only, normalized print exposure and no preflash. "
                    "Unit spectral integration weights are not a measured sensitivity curve. No absolute paper speed, "
                    "colour-negative printing, spectral filtration, variable contrast, tint or texture calibration.")
            papers[stock] = item
            continue
        target = info.get("target_print")
        # Upstream's cine B&W negative has no target; 2302 is the matching
        # supported B&W motion-picture print material. This pairing is our
        # recommendation, not an upstream per-film exposure calibration.
        if stock == "kodak_doublex":
            target = "kodak_2302"
            item["provenance"]["print_recommendation"] = "RAW Photo Editor: B&W motion-picture negative to B&W motion-picture print."
        item["target_print"] = None if info["type"] == "positive" else target
        if info["type"] == "negative" and (not target or stock not in catalog["compatible_films"].get(target, [])):
            raise ValueError(f"Unsupported recommended print for {stock}: {target}")
        values = catalog["couplers"].get(stock) if additional else legacy["_dir_coupler_defaults"](info)
        parameters = {matrix_keys[key]: list(value) for key, value in values.items()}
        halation = list(legacy["_halation_preset"](info)["strength"])
        if info["channel_model"] == "bw":
            halation = [halation[0]] * 3
        parameters["halationStrength"] = halation
        grain = grain_settings(profile, presets)
        mean_area = sum(areas[stock]) / 3
        # Preserve the exact physical RGB areas even outside the editable
        # scalar's 0.01..5 range: the native shader uses area * RGB scale.
        scalar_area = min(5.0, max(0.01, mean_area))
        parameters["grainParticleAreaUm2"] = scalar_area
        parameters["grainParticleScale"] = [area / scalar_area for area in areas[stock]]
        parameters["grainUniformity"] = rgb(grain["uniformity"])
        parameters["grainDensityMin"] = rgb(grain["density_min"])
        parameters["grainParticleScaleLayers"] = list(grain["particle_scale_sublayers"])
        item["parameters"] = parameters
        item["provenance"]["grain_rms_at_48um"] = rgb(grain["rms_granularity"])
        item["provenance"]["grain_calibration"] = ("Source preset RMS inverted over the reference-time density sublayers; "
            "fourth sublayer repeats the finest scale. Profile sublayers may be mathematical fit components.")
        if stock not in presets and not info.get("custom_look", {}).get("grain"):
            item["provenance"]["grain_calibration"] = "Upstream generic schema default; no stock-specific RMS preset is available."
        films[stock] = item
    return {"version": 1, "films": films, "papers": papers}


def pack():
    with (HERE / "stock_grain.toml").open("rb") as file:
        presets = tomllib.load(file)
    profiles = {}
    with tarfile.open(HERE / "core-source.tar.gz") as archive:
        legacy = legacy_defaults(archive)
        for member in archive:
            path = Path(member.name)
            if path.parent.as_posix() == "Resources/data/profiles" and path.suffix == ".json":
                profile = json.load(archive.extractfile(member))
                profiles[profile["info"]["stock"]] = profile
    with tarfile.open(HERE / "additional-stock-data.tar.gz") as archive:
        catalog = json.load(archive.extractfile("Resources/data/stock_catalog.json"))
        for stock in catalog["films"] + catalog["papers"]:
            profiles[stock] = json.load(archive.extractfile(f"Resources/data/profiles/{stock}.json"))
    areas = {stock: calibration_for(profile, presets) for stock, profile in profiles.items()
             if profile["info"]["stage"] == "filming"}
    (HERE / "stock_grain_areas.json").write_text(
        json.dumps(areas, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    defaults = recommended_defaults(profiles, catalog, presets, areas, legacy)
    (HERE / "stock_defaults.json").write_text(
        json.dumps(defaults, sort_keys=True, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"Generated recommendations for {len(defaults['films'])} films and {len(defaults['papers'])} print stocks")


if __name__ == "__main__":
    pack()
