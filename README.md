# RAW Photo Editor for Blender

A lightweight, open-source RAW photo editor for Blender. Develop, composite and batch-export photographs offline.

Each photo has its own Blender scene and compositor stack. Keep the original RAW file intact, revisit an adjustment, or add your own nodes before export. One installer (about 26 MB) includes the RAW decoder and Spektrafilm Lite for both supported platforms.

[Download the extension](https://github.com/shawnyhu/RAW-Photo-Editor/releases/latest) · [User guide](docs/workflow.md) · [Changelog](CHANGELOG.md)

## Workflow

- **Develop RAW files.** Set white balance and exposure, or load a camera profile. Work with smaller previews and export from the full-resolution original.
- **Crop and correct perspective.** Straighten to a fixed aspect ratio, place guides along architectural edges, or use automatic line detection.
- **Build a look.** Pair film and print stocks in Spektrafilm Lite, then use Blender’s compositor for masks and blends. The optional official Spektrafilm OFX plugin is installed separately.
- **Edit a series.** Copy adjustments between selected photos and batch-export the results.

## Installation

Requires **Blender 5.3 or later** on **Windows x64** or **macOS 14 or later on Apple Silicon**. Download `raw_photo_editor-1.0.1.zip` from Releases, choose **Preferences → Get Extensions → Install from Disk**, then select **Open Photo Workspace**.

If you used the earlier `blender_raw` extension, disable it in **Preferences → Add-ons** before enabling `raw_photo_editor`. Set any custom add-on preferences again in the new entry.

## Build from source

The matching `raw_photo_editor-source-*.zip` includes the Windows decoder wheel and native source archives. Extract it and run:

```sh
blender -b --factory-startup --offline-mode --command extension build --source-dir blender_raw
```

When building from a Git checkout without the Windows wheel, build it with the [decoder workflow](.github/workflows/prepare-decoder.yml) and place it in `blender_raw/wheels/`.

Maintained by **ShawnHu**. [GPL-3.0-or-later](LICENSE) · [Third-party licenses and credits](blender_raw/vendor/THIRD_PARTY.md)
