"""Check scanner target encoding and paper-specific calibration with the installed film engine."""
import numpy as np

from bl_ext.user_default.raw_photo_editor import spektrafilm_engine


def linear_srgb(value):
    return value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4


levels = np.array([0., .01, .18, 1., 4., 64., 1e4], dtype=np.float32)
chart = np.broadcast_to(np.repeat(levels, 32)[None, :, None], (32, 32 * len(levels), 3)).copy()


def scan(paper, *, low=None, high=None):
    effects = dict(scannerEnabled=True, scannerWhiteCorrection=high is not None,
                   scannerBlackCorrection=low is not None, glarePercent=0., scannerMtf50LpMm=0.,
                   scannerUnsharpAmount=0., cameraDiffusionEnabled=False, printDiffusionEnabled=False)
    if low is not None:
        effects["scannerBlackLevel"] = low
    if high is not None:
        effects["scannerWhiteLevel"] = high
    # With zero grain floor and no interlayer inhibition, the black and saturated
    # input patches develop to the two film-density references used by Metal.
    result = spektrafilm_engine.render(chart, film="kodak_portra_800", paper=paper, grain=False,
                                       halation=False, effects=effects,
                                       parameters={"grainDensityMin": (0., 0., 0.), "dirCouplersAmount": 0.})
    return result[16, 16::32, 1]


for paper in ("kodak_2383", "kodak_endura_premier"):
    black, white = .01, .98
    dark = scan(paper, low=black, high=black)
    light = scan(paper, low=white, high=white)
    # Two constant target levels preserve chromaticity. Their ratio cancels the
    # conversion from the paper's scan illuminant to linear Rec.709 RGB.
    ratio = dark / light
    expected = linear_srgb(black) / linear_srgb(white)
    raw = scan(paper) / light
    corrected = scan(paper, low=black, high=white) / light
    # Derive black and white independently from this paper's uncorrected pixels.
    # Using the measured target ratio makes this check independent of the transfer-curve test.
    relative = (raw - raw[0]) / (raw[-1] - raw[0])
    predicted = ratio + (1 - ratio) * relative
    print(f"{paper}: target ratio {ratio.mean():.8g} (expected {expected:.8g}), "
          f"maximum paper calibration error {np.max(np.abs(corrected - predicted)):.8g}", flush=True)
    np.testing.assert_allclose(ratio, expected, rtol=8e-4, atol=1e-7)
    np.testing.assert_allclose(corrected, predicted, rtol=8e-4, atol=2e-4)
