# SPDX-License-Identifier: GPL-3.0-or-later
"""Native effect parameters and the controls in the standalone Flow interface."""
from typing import NamedTuple


class Effect(NamedTuple):
    key: str
    label: str
    default: bool | int | float | tuple[float, ...]
    minimum: float
    maximum: float
    description: str = ""


# Identifiers and order match the upstream DiffusionFilterFamily enum.
FAMILIES = ("Glimmerglass", "Black Pro-Mist", "Pro-Mist", "CineBloom")
GRAIN_MODELS = ("Preview", "Production", "Grain Synthesis")
# Current Mac standalone 0.1.0 labels; numeric order matches the pinned App Bridge.
PROCESS_MODES = ("Full Film Sim", "Negative only", "Print only")
FIXED_EXPOSURES = frozenset(("filmExposureEv", "printExposureEv"))


def _diffusion(stage):
    prefix = stage + "Diffusion"
    parameters = [
        Effect(prefix + "Enabled", "Enabled", False, 0, 1,
               "Diffuse incoming light at the film" if stage == "camera" else
               "Diffuse the enlarger light during printing"),
        Effect(prefix + "Family", "Filter", 1, 0, 3, "Optical diffusion filter family"),
        Effect(prefix + "Strength", "Strength", 0.5, 0, 2, "Overall diffusion strength"),
        Effect(prefix + "SpatialScale", "Spatial Scale", 1.0, 0, 4, "Scale all diffusion radii"),
        Effect(prefix + "HaloWarmth", "Halo Warmth", 0.0, -1.5, 1.5, "Color balance of the halo"),
    ]
    for layer in ("Core", "Halo", "Bloom"):
        parameters.extend((
            Effect(prefix + layer + "Intensity", layer + " Intensity", 1.0, 0, 4),
            Effect(prefix + layer + "Size", layer + " Size", 1.0, 0.1, 4),
        ))
    return tuple(parameters)


GROUPS = {
    "dir": (
        Effect("dirCouplersAmount", "Amount", 1.0, 0, 2),
        Effect("dirCouplersDiffusionUm", "Diffusion (µm)", 20.0, 0, 100),
        Effect("dirCouplersDiffusionTailUm", "Tail (µm)", 200.0, 0, 1000),
        Effect("dirCouplersDiffusionTailWeight", "Tail Weight", 0.06, 0, 1),
        Effect("dirCouplersInhibitionSameLayer", "Same-Layer Multiplier", 1.0, 0, 2),
        Effect("dirCouplersInhibitionInterlayer", "Interlayer Multiplier", 1.0, 0, 2),
        Effect("dirGammaSameLayerRgb", "Same-Layer RGB", (0.336, 0.319, 0.273), 0, 1),
        Effect("dirGammaRToGb", "R to G/B", (0.353, 0.302), 0, 1),
        Effect("dirGammaGToRb", "G to R/B", (0.154, 0.353), 0, 1),
        Effect("dirGammaBToRg", "B to R/G", (0.168, 0.226), 0, 1),
    ),
    "grain": (
        Effect("grainAmount", "Amount", 1.0, 0, 2),
        Effect("grainSaturation", "Saturation", 1.0, 0, 1),
        Effect("grainParticleAreaUm2", "Particle Area (µm²)", 0.1, 0.01, 5),
    ),
    "halation": (
        Effect("halationAmount", "Amount", 1.0, 0, 4),
        Effect("halationScale", "Scale", 1.0, 0, 4),
        Effect("halationBoostEv", "Boost EV", 0.0, 0, 20),
        Effect("halationStrength", "Strength RGB", (0.05, 0.015, 0.0), 0, 1,
               "Fraction of halation light in each red, green and blue channel"),
    ),
    "camera": _diffusion("camera"),
    "print": _diffusion("print"),
    "scanner": (
        Effect("scannerEnabled", "Enabled", False, 0, 1, "Apply scanner corrections and optics"),
        Effect("scannerWhiteCorrection", "White Correction", False, 0, 1,
               "Map the paper's reference white to White Level"),
        Effect("scannerBlackCorrection", "Black Correction", False, 0, 1,
               "Map the paper's reference black to Black Level"),
        Effect("scannerWhiteLevel", "White Level", 0.98, 0, 1, "Scanner white target in sRGB encoding"),
        Effect("scannerBlackLevel", "Black Level", 0.01, 0, 1, "Scanner black target in sRGB encoding"),
        Effect("glarePercent", "Glare (%)", 0.03, 0, 0.2, "Average print glare in percent"),
        Effect("glareRoughness", "Glare Roughness", 0.7, 0, 4),
        Effect("glareBlur", "Glare Blur", 0.5, 0, 32, "Glare blur radius in pixels"),
        Effect("scannerMtf50LpMm", "MTF50 (lp/mm)", 60.0, 0, 300,
               "Spatial frequency at half contrast in line pairs per millimeter, or zero to disable"),
        Effect("scannerUnsharpRadiusUm", "Unsharp Radius (µm)", 5.0, 0, 100),
        Effect("scannerUnsharpAmount", "Unsharp Amount", 0.7, 0, 4),
    ),
}

BY_KEY = {param.key: param for group in GROUPS.values() for param in group}
DIR_MATRIX_PARAMETERS = tuple(p.key for p in GROUPS["dir"] if isinstance(p.default, tuple))

FLOW_SECTIONS = (
    ("grain", "Grain", "grain", ("grain_model", "grainAmount", "grainSaturation", "grainParticleAreaUm2")),
    ("halation", "Halation", "halation", ("halationAmount", "halationScale", "halationBoostEv", "halationStrength")),
    ("camera", "Camera Diffusion", "cameraDiffusionEnabled",
     ("cameraDiffusionFamily", "cameraDiffusionStrength", "cameraDiffusionHaloWarmth")),
    ("scanner", "Scanner", "scannerEnabled",
     ("scannerWhiteCorrection", "scannerBlackCorrection", "scannerWhiteLevel", "scannerBlackLevel")),
)
