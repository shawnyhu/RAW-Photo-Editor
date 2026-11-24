# Camera color data

The offline catalog records two daylight matrices and two cameras that use embedded DNG calibration. Users can select an external DCP in Raw Developer.

## Daylight matrices

The Canon EOS R8 Mark II and Sony ILCE-7RM6 records come from RAWmakase's [exact-model matrix table](https://github.com/pch/rawmakase/blob/3c5441375a8a55c081cf8976cfc94f7f97c86ed2/data/raw-matrices.toml). Copyright (c) 2026 RAWmakase contributors. The [MIT license](LICENSE.RAWmakase) accompanies this directory. Upstream identifies both records as the D65 ColorMatrix from a converted DNG. The index stores the nine XYZ-to-camera coefficients in row-major order, converted from ten-thousandths to decimal values, with DNG illuminant 21 (D65).

## Embedded DNG calibration

The Leica M10-R and Sigma FP2 entries select the calibration stored in each photograph's DNG metadata. Their dual-illuminant matrices travel with the photograph. These two index entries contain model identifiers only.
