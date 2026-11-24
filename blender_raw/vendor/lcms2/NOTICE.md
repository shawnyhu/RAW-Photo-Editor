# Little CMS for user-supplied camera input profiles

The installer includes the Little CMS 2.20 core shared library for both supported platforms. Its author is Marti Maria Saguer. The [upstream MIT license](LICENSE) accompanies these unmodified libraries. The Windows DLL imports KERNEL32, and the macOS library links to libSystem. [Upstream source](https://github.com/mm2/Little-CMS/tree/lcms2.20)

| Platform | Fixed conda-forge distribution | Package SHA-256 | Bundled library SHA-256 |
|---|---|---|---|
| Windows x64 | [lcms2-2.20-hf2c6c5f_0](https://conda.anaconda.org/conda-forge/win-64/lcms2-2.20-hf2c6c5f_0.conda) | `7389c5b7fafa0ff27466df34fdadb70c726ad4377f0d97aed8edbc7d0a948155` | `604f26a5b125f7afed01606b2e459e4d072836851cebfcb4eae795ad4e98f8ed` |
| Apple Silicon Mac | [lcms2-2.20-hce41798_0](https://conda.anaconda.org/conda-forge/osx-arm64/lcms2-2.20-hce41798_0.conda) | `a5474fb5eea0d8d7219c29362df161327f09f02cb32f11c5ab4fa7460fe7b356` | `5628b18b17cda2589befe90611cfc9d567f7b0031acc5f9a0ae8f1f1f8de19ae` |

The package payload paths are `Library/bin/lcms2.dll` on Windows and `lib/liblcms2.2.dylib` on Mac. Both distributions carry the same license in `info/licenses/LICENSE`.
