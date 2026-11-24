"""Check each Flow switch and a parameter independently using the installed GPU renderer."""
import numpy as np

from bl_ext.user_default.raw_photo_editor import spektrafilm_engine


y, x = np.mgrid[:192, :256].astype(np.float32)
rgb = np.stack((.02 + .8 * x / 255, .06 + .4 * y / 191, .04 + .3 * x / 255), axis=-1)
rgb[45:52, 32:170] = [6, 4, 2]
rgb[75:150, 125:138] = [2, 5, 8]
rgb[112:158, 24:77] = .18


def render(**controls):
    options = dict(film="kodak_portra_800", paper="kodak_2383", frame=1.0,
                   grain=controls.pop("grain", False), grain_model=controls.pop("grain_model", None),
                   halation=controls.pop("halation", False))
    if "grainSynthesisAmount" in controls:
        # Synthesis uses the dedicated preset parameter in both native backends.
        options["parameters"] = {"grainSynthesisAmount": controls.pop("grainSynthesisAmount")}
    options["effects"] = {"cameraDiffusionEnabled": False, "scannerEnabled": False, "glarePercent": 0., **controls}
    result = spektrafilm_engine.render(rgb, **options)
    assert result.shape == rgb.shape and np.isfinite(result).all()
    return result


for name, switch, setting, low, high, extra in (
    ("Preview Grain", "grain", "grainAmount", 0.5, 1.5, {"grain_model": 0}),
    ("Production Grain", "grain", "grainAmount", 0.5, 1.5, {"grain_model": 1}),
    ("Grain Synthesis", "grain", "grainSynthesisAmount", 0.5, 1.5, {"grain_model": 2}),
    ("Halation", "halation", "halationAmount", 1., 3., {"halationBoostEv": 2.}),
    ("Camera Diffusion", "cameraDiffusionEnabled", "cameraDiffusionStrength", 0.6, 1.8, {}),
    ("Scanner", "scannerEnabled", "scannerWhiteLevel", .93, .67, {"scannerWhiteCorrection": True}),
):
    off = render(**{switch: False, setting: low}, **extra)
    enabled = render(**{switch: True, setting: low}, **extra)
    adjusted = render(**{switch: True, setting: high}, **extra)
    toggle_delta = np.abs(enabled - off)
    parameter_delta = np.abs(adjusted - enabled)
    print(f"{name}, frame 1: toggle mean={toggle_delta.mean():.8g} max={toggle_delta.max():.8g}, "
          f"parameter mean={parameter_delta.mean():.8g} max={parameter_delta.max():.8g}", flush=True)
    assert toggle_delta.max() > 1e-5 and parameter_delta.max() > 1e-5, name
