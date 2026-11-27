# Spektrafilm renderer for RAW Photo Editor

The Apple Silicon Metal and Windows x64 Vulkan builds use Aedan Diez's GPL-3.0 renderers from
[spektrafilm-ofx at 86476af](https://github.com/chaert-s/spektrafilm-ofx/tree/86476afc5b077de77e2278e3658d1ba9309892a1),
based on the [spektrafilm simulation by Andrea Volpato](https://github.com/andreavolpato/spektrafilm).
The original 20 film and eight paper tables derive from Andrea Volpato's profiles, licensed
CC BY-SA 4.0. See `SPEKTRAFILM_LICENSE.txt` and the upstream attribution in
`THIRD_PARTY_NOTICES.txt`. The table generator uses Colour's colorimetric and
spectral reference data (`LICENSE.colour-science`).

**CIE standard observer.** The renderer includes the x̄, ȳ and z̄ matching functions
of the CIE 1931 2° observer. Source: International Commission on Illumination (CIE),
Vienna, 2019, [dataset DOI 10.25039/CIE.DS.xvudnb9b](https://doi.org/10.25039/CIE.DS.xvudnb9b).
The generator selects 380–780 nm at 5 nm intervals via Colour, converts the
samples to numeric arrays and compiles them into the renderer. These sampled
tables are distributed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/),
as specified in the [dataset metadata](https://files.cie.co.at/Publications-datasets/CIE_xyz_1931_2deg.csv_metadata.json). The project GPL text is in
the extension's LICENSE.txt and in the source archive.

RAW Photo Editor generated the runtime tables from the public source data,
added the small `photo_bridge.mm` entry point, and changed resource lookup and
shader compilation in `extension.patch`. Original profile values are preserved.
The public source provides filtered-enlarger print timing. This package is an
independent build. It uses no resources extracted from installed apps or OFX
distributions.

Additional colour film and motion-picture print stocks come from
[Negs by Noah Haasis](https://github.com/noahhaasis/negs/tree/2f5b5703d807e9490801d9155ac30b2c19460551/data).
Their profiles and calibration data are CC BY-SA 4.0. They include work by Noah
Haasis, Andrea Volpato and MUAStudio Inc. (Fotufilm). Each JSON records the
individual author, measurements, source and fitting assumptions. See the
[stock changelog](CHANGELOG.txt) and the upstream notices under
`Legal/Negs/` in `additional-stock-data.tar.gz`. Four further stocks come from the
CC BY-SA 4.0 profiles in [The Dyeworks 2.2.3 by Celian Le Bolloch](https://github.com/natcelian/spektrafilm.exe/releases/tag/2.2.3).
See `Legal/Dyeworks/` in the same archive.

Both native builds extract that archive and apply `stock-tables.patch` to the
pinned generator. Additional film and print IDs are appended. Their TH-KG3
enlarger filters are required for every selectable pair. Previously shipped
pairs use their original table and original indices. To recreate this data
archive from the pinned Negs checkout and the Dyeworks release data, run
`python pack_stock_data.py /path/to/negs /path/to/dyeworks-input`. The second
directory contains the four stock JSONs, `neutral_print_filters.json`, `couplers.toml`
and the release's `CHANGELOG.txt` and `SPEKTRAFILM_LICENSE.txt`. Run
`python pack_stock_grain.py` to regenerate the new stocks' particle areas from
the supplied layer curves and RMS granularity using the
[upstream grain model](https://github.com/andreavolpato/spektrafilm/blob/28bf883e1672e884307edc75852549376e13644e/src/spektrafilm/model/grain.py).

For the four original slide stocks, the Python adapter supplies the stock-specific DIR
matrices from the pinned table generator's `_dir_coupler_defaults()` when the
recipe does not explicitly provide them. This avoids applying the app bridge's
fixed negative-film matrix to reversal film. Preset matrix overrides are
preserved. The adjustment is shared by Metal and Vulkan. The original profile
values remain unchanged when rebuilding the native libraries with added stocks.

### Rebuilding for Apple Silicon

`core-source.tar.gz` contains the corresponding upstream Objective-C++ code,
Metal kernel, native headers, table generator, and its source profiles and tables.
On an Apple Silicon Mac with the macOS command-line SDK, patch, and Python 3.13,
install `numpy==2.5.5 scipy==1.18.1 colour-science==0.4.7` in a build environment.
Run `python build_macos.py` in this directory. The script extracts and patches a
temporary copy, regenerates the runtime resources and compiles an ad-hoc signed
arm64 dylib targeting macOS 14 or later. Users need only the files distributed in
the extension. Metal compiles the included kernel source on the installed GPU.

### Rebuilding for Windows x64

`vulkan-source.tar.gz` contains the upstream Vulkan renderer, its ten GLSL sources,
and `SpektraVulkanCopyHarness.cpp` from the same revision as `core-source.tar.gz`.
`windows.patch` connects the shared app bridge to Vulkan, loads resources relative
to the DLL, checks device limits, and exposes the device used by the renderer.
Its print/scan shader computes black and white reference luminances for the
active film and paper and interprets the scanner levels in sRGB, as Metal does.
Images over 2,097,152 pixels use upstream overlapping tiles. Vulkan downloads only
the required rows from each tile and copies its center directly into the final image.
Grain Synthesis uses
the upstream full-frame path and its memory requirements depend on image size.

Use 64-bit Python 3.13, Git, [LLVM-MinGW UCRT x86_64](https://github.com/mstorsjo/llvm-mingw/releases/tag/20261117),
and the Vulkan SDK. Set `VULKAN_SDK` to the SDK root, put the compiler and SDK
binary directories on PATH, then run from this directory:

```powershell
py -3.13 -m venv build-env
.\build-env\Scripts\python -m pip install numpy==2.5.5 scipy==1.18.1 colour-science==0.4.7
.\build-env\Scripts\python build_windows.py --test
```

The distributed binary used Clang 23.1.6, [glslang 16.6.0](https://github.com/KhronosGroup/glslang/releases/tag/16.6.0),
and [Vulkan headers 1.4.341.0](https://github.com/KhronosGroup/Vulkan-Headers/tree/vulkan-sdk-1.4.341.0).
For standalone SDK components, set `--glslang glslang`, `--vulkan-include` to
the headers' `include` directory, and `--vulkan-lib` to the loader import library.
The import library can be built from the [loader export list](https://github.com/KhronosGroup/Vulkan-Loader/blob/vulkan-sdk-1.4.341.0/loader/vulkan-1.def)
with `llvm-dlltool -m i386:x86-64 -D vulkan-1.dll -d vulkan-1.def -l vulkan-1.lib`.
The script checks both generated float tables against the shipped Mac tables. With
`--test` it runs the upstream copy/core tests, compares a multi-tile render with
full-frame GPU output, then probes the built DLL.

The extension contains `photo_film.dll`, compiled SPIR-V in `shaders/`, and
the two shared float tables. The DLL links the C++ runtime statically and uses
the system Vulkan loader supplied with the graphics driver. The build tools and
SDK are needed only when rebuilding. Runtime notices accompany the binary in
`LICENSE.llvm` and `LICENSE.mingw-w64`.
