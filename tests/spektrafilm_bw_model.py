"""Validate the native B&W model against its licensed scalar source data.

Run with the native build environment (numpy, scipy, colour-science):
    python tests/spektrafilm_bw_model.py --report report.json

This extracts the shipped sources and applies the build patches in a temporary
directory. It needs no Blender, GPU, source checkout, or network connection.
"""
import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from tempfile import TemporaryDirectory

import numpy as np


VENDOR = Path(__file__).resolve().parents[1] / "blender_raw/vendor/spektrafilm"
# Independently transcribed source defaults; profiles without an explicit
# default use Negs' floor-middle development_time family member.
REFERENCE_MINUTES = {
    "kodak_doublex": 6.5, "kodak_trix": 1.0, "ilford_hp5_plus": 6.5,
    "ilford_fp4_plus": 8.0, "ilford_delta_3200": 9.0, "kodak_tmax_100": 6.0,
    "fujifilm_acros_100_ii": 7.0, "kodak_trix400_d76_8min": 8.0,
    "kodak_2302": 5.0, "ilford_mgrc_deluxe_grade2": 1.0,
}


def import_generator(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def arrays(source):
    return {name: [value.strip() for value in body.split(",") if value.strip()]
            for name, body in re.findall(r"constexpr float (\w+)\[\] = \{(.*?)\};", source, re.S)}


def check_color_tables(previous, current):
    checked = 0
    for group, stocks in (("film", current.FILMS[:56]), ("paper", current.PAPERS[:12])):
        old = arrays(previous._emit_group(group, stocks))
        new = arrays(current._emit_group(group, stocks))
        assert old.keys() == new.keys()
        for name, values in old.items():
            actual = new[name]
            if name.endswith("_density_curve_layers"):
                padded = np.asarray(actual).reshape(-1, 4, 3)
                assert (padded[:, 3] == "0.0f").all(), name
                actual = padded[:, :3].reshape(-1).tolist()
            elif name.endswith("_density_curve_layer_maxima"):
                assert actual[9:] == ["0.0f"] * 3, name
                actual = actual[:9]
            assert actual == values, name
            checked += 1
    return checked


def check_bw(current, root):
    catalog = current.ADDITIONAL
    grain_presets = tomllib.loads((VENDOR / "stock_grain.toml").read_text(encoding="utf-8"))
    grain_areas = json.loads((VENDOR / "stock_grain_areas.json").read_text(encoding="utf-8"))
    stocks = catalog["bw_films"] + catalog["bw_papers"]
    assert set(stocks) == set(REFERENCE_MINUTES)
    report = []
    for stock in stocks:
        original = json.loads((root / "Resources/data/profiles" / f"{stock}.json").read_text(encoding="utf-8"))
        data, info = original["data"], original["info"]
        resolved = current._load_profile(stock)["data"]
        requested = REFERENCE_MINUTES[stock]
        assert requested == catalog["bw_reference_time"][stock]
        assert resolved["selected_development_time"] == requested
        index = data["development_time"].index(requested)
        fit = data["density_curves_model"]
        assert fit["model_type"] in {"cdfs", "norm_cdfs"}
        sign = -1 if info["type"] == "positive" else 1
        # Independent scalar transcription of Negs print_morph::norm_cdf:
        # libm erfc here, vectorized scipy.special.ndtr in the generator.
        layers = np.asarray([
            [amplitude * .5 * math.erfc(-sign * (x - center) / sigma / math.sqrt(2))
             for center, amplitude, sigma in zip(fit["centers"][index],
                                                fit["amplitudes"][index], fit["sigmas"][index])]
            for x in data["log_exposure"]])
        curves = layers.sum(axis=1)
        assert sign * (curves[-1] - curves[0]) > .1, stock
        np.testing.assert_allclose(resolved["density_curves"], np.repeat(curves[:, None], 3, axis=1),
                                   rtol=2e-14, atol=2e-15, err_msg=stock)
        np.testing.assert_allclose(resolved["density_curves_layers"], np.repeat(layers[:, :, None], 3, axis=2),
                                   rtol=2e-14, atol=2e-15, err_msg=stock)
        np.testing.assert_allclose(np.asarray(resolved["density_curves_layers"]).sum(axis=1),
                                   resolved["density_curves"], rtol=0, atol=1e-15, err_msg=stock)
        if layers.shape[1] == 4:
            assert np.max(layers[:, 3]) > .01, stock
        if stock in catalog["bw_films"]:
            # Independent scalar inversion of the layer-particle variance.
            # BW has one physical channel; its density/area is never divided
            # by three. The fourth layer repeats the finest supplied scale.
            grain = grain_presets["defaults"]["bw"][info["type"]] | grain_presets.get(stock, {})
            maxima = layers.max(axis=0)
            fractions = maxima / maxima.sum()
            floor = fractions * grain["density_min"][0]
            density = layers + floor
            source_scales = grain["particle_scale_sublayers"]
            scales = np.asarray([source_scales[min(i, len(source_scales) - 1)] for i in range(layers.shape[1])])
            variance = scales / fractions * density * (maxima + floor - grain["uniformity"][0] * density)
            area = (grain["rms_granularity"][0] / 1000) ** 2 * math.pi * 24 ** 2 / variance.sum(axis=1).max()
            np.testing.assert_allclose(grain_areas[stock], [area] * 3, rtol=2e-14, atol=1e-14, err_msg=stock)
        sensitivity = np.asarray(data["log_sensitivity"], float)[:, 0]
        silver = np.asarray(data["channel_density"], float)[:, 0]
        base = np.asarray(data["base_density"], float)[:, index]
        np.testing.assert_array_equal(resolved["base_density"], base)
        np.testing.assert_array_equal(np.asarray(resolved["log_sensitivity"], float),
                                      np.repeat(sensitivity[:, None], 3, axis=1))
        # Arbitrary spectra exercise scalar integration beyond a gray chart.
        spectra = np.random.default_rng(7231).uniform(0, 5, (17, len(sensitivity)))
        scalar_exposure = spectra @ np.nan_to_num(10 ** sensitivity)
        embedded_exposure = spectra @ np.nan_to_num(10 ** np.asarray(resolved["log_sensitivity"], float))
        np.testing.assert_allclose(embedded_exposure, np.repeat(scalar_exposure[:, None], 3, axis=1),
                                   rtol=2e-15, atol=1e-12, err_msg=stock)
        density = np.linspace(0, 3.5, 17)
        scalar_density = density[:, None] * silver[None, :] + base[None, :]
        embedded_density = (np.repeat(density[:, None], 3, axis=1)
                            @ np.asarray(resolved["channel_density"]).T + base[None, :])
        np.testing.assert_array_equal(scalar_density, embedded_density, err_msg=stock)
        # Scan matrices must use spectral CIE Y identically in all RGB lanes,
        # for every supported output color space and both film/print scans.
        group = "film" if stock in catalog["bw_films"] else "paper"
        tables = arrays(current._emit_group(group, [stock]))
        matrix = tables[f"{group}_0_{stock}_scan_to_output_rgb"]
        assert matrix == ["0.0f", "1.0f", "0.0f"] * (len(matrix) // 3), stock
        cached = np.asarray(data["density_curves"], float)[:, index]
        cached_layers = np.asarray(data["density_curves_layers"], float)[:, :, index]
        report.append({"stock": stock, "development_minutes": requested,
                       "physical_layers": layers.shape[1], "type": info["type"],
                       "cached_total_error_od": float(np.max(abs(cached - curves))),
                       "cached_layer_sum_error_od": float(np.max(abs(cached_layers.sum(axis=1) - curves)))})
    assert {row["physical_layers"] for row in report} == {3, 4}
    assert any(row["type"] == "positive" for row in report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    with TemporaryDirectory(prefix="spektrafilm-bw-model-") as folder:
        root = Path(folder)
        for name in ("core-source.tar.gz", "vulkan-source.tar.gz", "additional-stock-data.tar.gz"):
            with tarfile.open(VENDOR / name) as archive:
                archive.extractall(root, filter="data")
        subprocess.run(["git", "apply", "--no-index", str(VENDOR / "stock-tables.patch")], cwd=root, check=True)
        generator = root / "tools/generate_profile_curves.py"
        previous = generator.with_name("previous_generator.py")
        shutil.copyfile(generator, previous)
        subprocess.run(["git", "apply", "--no-index", str(VENDOR / "bw-stock-model.patch")], cwd=root, check=True)
        os.environ["SPEKTRAFILM_DATA_DIR"] = str(root / "Resources/data")
        os.environ.pop("SPEKTRAFILM_PROFILE_DIR", None)
        os.environ.pop("SPEKTRAFILM_ARCHIVE_PROFILE_DIR", None)
        sys.path.insert(0, str(root / "tools"))
        before = import_generator(previous, "previous_generator")
        current = import_generator(generator, "current_generator")
        checked = check_color_tables(before, current)
        bw = check_bw(current, root)
    result = {"status": "PASS source model; no GPU pixels tested",
              "unchanged_color_profiles": 68, "unchanged_color_arrays": checked, "bw_profiles": bw}
    if args.report:
        args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"PASS {checked} unchanged color tables, 10 scalar B&W models, development time, layers and neutral scans")


if __name__ == "__main__":
    main()
