"""Exercise official OFX isolation, data encoding, native controls and defaults.

Run with Blender's Python: tests/ofx_engine.py --plugin /path/to/complete/release
The official bundle is external and is never copied into the extension.
"""
import argparse
import json
from pathlib import Path
import sys
import tempfile

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender_raw"))
import ofx_schema as schema
import spektrafilm_ofx_engine as engine


def check(condition, description):
    if not condition:
        raise AssertionError(description)
    print("PASS", description, flush=True)


def raw_render(image, parameters, plugin, folder):
    source, result, request, meta = [folder / name for name in ("in.f32", "out.f32", "request.json", "meta.json")]
    image.tofile(source)
    params = engine._linear_input(dict(parameters))
    request.write_text(json.dumps(dict(width=image.shape[1], height=image.shape[0], frame=1.0,
                                      parameters=params, result_metadata=str(meta))), encoding="utf-8")
    engine._run(["render", engine.discover_plugin(plugin), request, source, result])
    return np.fromfile(result, np.float32).reshape(image.shape)


def main(plugin):
    for p in schema.PARAMETERS:
        engine.validate_parameter(p.key, p.default)
    check(schema.SCHEMA_VERSION == "0.5.0", "schema is the actual official 0.5.0 interface")
    check(len(engine.builtin_looks(plugin)) == 60, "all 60 official clean and creative looks enumerated")
    check(len(set(name for item in schema.CHOICE_DEPENDENCIES['film']['values'].values()
                  for name in item['options'])) >= 35, "all film categories retain distinct native stocks")
    check(not any(p.key.startswith(("quick", "playback")) for p in schema.PARAMETERS), "alias controls excluded")
    for values in ({"filmExposureEv": float('nan')}, {"film": True}, {"filmExposureEv": 200},
                   {"inputColorSpace": 0}, {"unknownParameter": 1}, {"filmCategory": 4, "film": 24}):
        try:
            engine.normalize_parameters(values)
        except ValueError:
            continue
        raise AssertionError(f"Invalid recipe accepted: {values}")
    check(engine.backend_error("C:/does-not-exist/spektrafilm.ofx.bundle") != "", "missing plugin reports setup error")
    image = np.ones((96, 128, 4), np.float32)
    x = np.linspace(0.002, 1.8, image.shape[1], dtype=np.float32)[None, :]
    y = np.linspace(0.4, 1.1, image.shape[0], dtype=np.float32)[:, None]
    image[..., 0], image[..., 1], image[..., 2] = x * y, x * 0.7, x * (1.2 - y * 0.5)
    image[36:40, 49:53, :3] = 9
    base_params = {"grainSeed": 0}
    base = engine.render_rgba(image, parameters=base_params, plugin_path=plugin)
    again = engine.render_rgba(image, parameters=base_params, plugin_path=plugin)
    check(np.array_equal(base, again), "default still render repeats exactly across processes")
    stock_a = engine.render_rgba(image, parameters={"film": 0, "filmCategory": 0}, plugin_path=plugin)
    stock_b = engine.render_rgba(image, parameters={"filmCategory": 0, "film": 0}, plugin_path=plugin)
    check(np.array_equal(stock_a, stock_b), "stock callback ordering survives sorted JSON recipes")
    cases = {
        "Film EV": {"filmExposureEv": 1.0},
        "Print EV": {"printExposureEv": 1.0},
        "Motion picture stock": {"filmCategory": 0, "film": 0},
        "RMS grain": {"grainEnabled": True, "grainSeed": 0},
        "Halation": {"halationEnabled": True, "halationAmount": 3.0},
        "Camera diffusion": {"cameraDiffusionEnabled": True, "cameraDiffusionStrength": 1.5},
        "Print diffusion": {"printDiffusionEnabled": True, "printDiffusionStrength": 1.5},
        "Lens distortion": {"lensDistortionEnabled": True, "lensDistortionPreset": 3},
        "Bokeh": {"bokehEnabled": True, "bokehPreset": 4},
        "Film gate": {"filmGateEnabled": True, "filmGateFormatPreset": 9},
    }
    for label, parameters in cases.items():
        output = engine.render_rgba(image, parameters={**base_params, **parameters}, plugin_path=plugin)
        difference = float(np.max(np.abs(output[..., :3] - base[..., :3])))
        check(np.isfinite(output).all() and difference > 1e-5, f"{label} affects actual rendered pixels ({difference:.6g})")
    with tempfile.TemporaryDirectory(prefix="photo-ofx-test-") as folder:
        folder = Path(folder)
        raw = raw_render(image, base_params, plugin, folder)
        check(np.allclose(base[..., :3], np.maximum(raw[..., :3], 0) ** 2.4, atol=2e-6),
              "SDR adapter decodes actual official Rec.709 2.4 samples")
        params = {"outputRole": 1, "hdrPreset": 3, "hdrTransfer": 0, "hdrPeakNits": 1000.0,
                  "hdrReferenceWhiteNits": 203.0, "grainSeed": 0}
        pq = engine.render_rgba(image, parameters=params, plugin_path=plugin)
        hlg = engine.render_rgba(image, parameters={**params, "hdrTransfer": 1}, plugin_path=plugin)
        maximum = float(np.max(np.abs(pq[..., :3] - hlg[..., :3])))
        check(np.allclose(pq[..., :3], hlg[..., :3], atol=2e-4, rtol=2e-4),
              f"PQ and HLG decode to matching actual luminance ({maximum:.6g})")
        neutral = np.ones((16, 32, 4), np.float32)
        neutral[..., :3] = np.linspace(0.02, 0.4, 32)[None, :, None]
        reference = engine.render_rgba(neutral, parameters={"sdrOutputColorSpace": 8,
                                       "colorAdaptationOutputGamutMode": 0}, plugin_path=plugin)
        for encoding in range(8):
            result = engine.render_rgba(neutral, parameters={"sdrOutputColorSpace": encoding,
                                        "colorAdaptationOutputGamutMode": 0}, plugin_path=plugin)
            check(np.allclose(result, reference, atol=1e-6), f"SDR encoding {encoding} agrees after native gamut conversion")
        for encoding in (1, 2, 3):
            params = {"process": 1, "negativeScanOutput": encoding, "grainSeed": 0}
            direct = raw_render(image, params, plugin, folder)
            adapted = engine.render_rgba(image, parameters=params, plugin_path=plugin)
            check(np.array_equal(direct[..., :3], adapted[..., :3]),
                  f"negative data encoding {encoding} remains bit-exact")
    selected = engine.builtin_preset(1, 0, plugin)
    check(len(selected) > 500, "official bundled preset resolves to a full native parameter snapshot")
    check(engine.cache_identity(plugin)[2] == engine.discover_plugin(plugin), "cache identifies the resolved external bundle")
    wide = np.ones((64, 8448, 4), np.float32)
    wide[..., :3] = np.linspace(0.02, 0.7, 8448)[None, :, None]
    rendered = engine.render_rgba(wide, plugin_path=plugin)
    check(np.isfinite(rendered).all() and not np.any(np.all(rendered[..., :3] == 0, axis=(0, 2)))
          and rendered[32, -1, 0] > 0.1, "8448-pixel-wide image retains both edges without blank columns")
    print("OFX ENGINE TESTS PASSED")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin", required=True)
    main(parser.parse_args().plugin)
