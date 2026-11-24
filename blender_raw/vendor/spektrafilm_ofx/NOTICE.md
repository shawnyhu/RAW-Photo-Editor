# RAW Photo Editor OFX host

This helper loads an externally installed, unmodified Spektrafilm OFX bundle.
No official plugin binaries, shaders, profile tables, presets or other plugin
resources are redistributed in this directory. The host does not use the older
Spektrafilm Lite processing library.

The `src/ofx/` host suites, `src/core/` utilities and `src/platform/Paths.h` are
adapted from [OFX Raw Host](https://github.com/aaronmurniadi/ofxrawhost), commit
`cc5868e2dbd7725b6fa7e82e580ab25fcf5d4293`, copyright Aaron P. Murniadi, BSD-3-Clause.
The original license is in `LICENSE.ofxrawhost`. Modifications expose loading one
explicit bundle, identify RAW Photo Editor as the host, pass frame time,
support JSON Unicode escapes and join suite threads at the end of their work.

The `include/` OpenFX headers are from the Academy Software Foundation OpenFX
repository, commit `ab779510b2655b4d11a7e01e5c521f9aa8c88976`. See `LICENSE.openfx`
and the notices in the individual headers. `src/photo_ofx_host.cpp` and `build.py`
are RAW Photo Editor additions under GPL-3.0-or-later.

## Build

Run `python build.py --cxx /path/to/clang++`. Windows uses LLVM-MinGW x86_64;
Apple Silicon uses Xcode command-line tools and targets macOS 14. The included
Mac executable is ad-hoc signed and built by `.github/workflows/ofx-macos.yml`.
An Apple Silicon macOS 15 runner verified its architecture and signature, loaded
official OFX 0.5.0 and rendered a Metal frame with an exposure-response check.
The adapter restores owner execute permission if installation strips it; this
does not change the binary's contents or signature.
The compiled helper needs no Python packages, Vulkan SDK or GUI toolkit. The
selected external plugin supplies its own GPU rendering implementation.

The offline descriptor contains the identifiers, types, bounds, defaults and stock choices needed to open and edit image recipes. The help button reads text from the locally installed plug-in. Built-in Looks reads the local factory menu when opened. The extension stores neither the native help text nor the factory look catalog in its distribution.

## Protocol

`photo_ofx_host describe BUNDLE OUTPUT.json [REQUEST.json]` queries the actual
plugin and creates an instance to obtain effective defaults. Optional request
parameters execute native parameter-change callbacks before inspection.

`photo_ofx_host render BUNDLE REQUEST.json INPUT.f32 OUTPUT.f32` renders one frame.
The request contains `width`, `height`, `frame` and an ordered `parameters` object.
Optional `result_metadata` writes effective metadata after native callbacks.
Files contain native-endian float32, top-down, straight RGBA; the helper converts
row order to/from OFX's bottom-left image origin. Unknown parameters, wrong
dimensions, out-of-range values and non-finite results fail explicitly.

`photo_ofx_host export-lut BUNDLE REQUEST.json OUTPUT.cube` invokes the official
plugin's LUT export callback. `lut_size` selects 33 or 65; the same sparse
`parameters` object selects the look. Native export is staged under the request
directory, then its generated cube and exact official LUT license are copied to
the requested destination (`OUTPUT.cube.LICENSE.txt`). Existing files are rejected
unless `overwrite` is true. The plugin itself excludes spatial effects from the
LUT. LUT values retain the native output encoding named in the file header.

Host-specific modal actions such as plugin update dialogs and plugin-managed
preset dialogs are not invoked through the general numeric parameter protocol.
Blender supplies its own preset and file-selection UI. GPU failures are isolated
to the worker process.

When `BPE_OFX_PARENT_PID` is set, the helper exits immediately if that worker
process dies, so cancelling a preview also stops its native GPU work. Windows
uses a process identity handle; macOS checks parent identity at 50 ms intervals.
The helper destroys its OFX instance, joins its watchdog and closes all output
files before ending the process directly. This avoids waiting for the official
0.5.0 plugin's asynchronous update-check task during DLL/static teardown. The
worker cannot update plugin binaries and never ends its parent process.
