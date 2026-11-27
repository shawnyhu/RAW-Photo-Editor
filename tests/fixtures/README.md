# Auto Level legacy fixture

`auto_level_beta7.blend` was generated with the published beta.7 installed extension (commit `89d5c7f`) in Blender 5.3.0. It contains a packed, synthetic float EXR ramp with endpoints −0.055117 and 0.95018, one automatic card and one disabled manual card with an animated black point. It contains no user photograph. The synthetic pixel data are dedicated to the public domain (CC0-1.0).

The unmodified beta.7 graph maps an input of zero to 0.054826587. `tests/auto_level_black_floor.py` opens this file with the current extension, without rebuilding its graph or reapplying a recipe, and checks the first render, retained manual values/animation and idempotent migration.
