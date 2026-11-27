# Changelog

## 1.0.2-beta.5 — 2026-11-27

- Spektrafilm Lite ships four built-in presets: **FUJI Pro 400H**, **FUJI Provia100f**, **KODAK Ektar 100** and **KODAK Portra 800**. Open **Presets → Built-in Presets** on any Lite node to apply one. Save an edited version under a name in your personal library.
- The Film and Print selectors now contain **56 films and 12 print materials**. Added 32 film profiles from Negs, Kodak Vision2 50D/100T/500T and UltraMax 800 from The Dyeworks, and four Fujifilm Eterna-CP print films. Selecting an added film loads its DIR, halation and grain defaults together with its compiled spectral and development data; selecting print material uses the corresponding calibrated print filters. All data ship with Lite for offline use.
- Retained the original 20 film and 8 print indices. Added profiles include their source metadata, licenses and adaptation notes. UltraMax 800 uses its own characteristic curves with UltraMax 400 spectral data; rendered results follow Lite's model.

## 1.0.2-beta.4 — 2026-11-27

- In **Spektrafilm Lite → Presets**, save the current node settings under a name, apply a saved look, rename it or delete it. The library is shared across projects and Blender versions on the same user account. Applying a look preserves the destination node's Live Preview setting.
- Grid starts off in newly created Photo Workspace image viewers. The viewer saves subsequent changes to Grid with the project.

## 1.0.2-beta.3 — 2026-11-27

- In the photo editor, **Add Scene Effect → Adjust** contains nine native color adjustment cards. Spektrafilm Lite and OFX have their own section. Use **Custom Compositor** for an editable node group.
- **Raw Developer** groups its controls under **Camera & Look**, **White Balance** and **Auto Exposure**. Exposure compensation is the first control in Auto Exposure. Turn off **As Shot** to start in **Spot** metering mode. Existing exposure values and keyframes keep their original socket.
- In the photo viewer sidebar, open **Editor → Canvas Background** to choose black, dark gray or light gray. The workspace saves the selected color. Black is the default.

## 1.0.2-beta.2 — 2026-11-26

- Choose **Vertical**, **Horizontal**, or **Both** in Crop & Perspective. **Edit Guides** and **Auto Detect** use the selected direction. Existing crops open with their original four-guide correction.
- Auto Detect checks for well-supported, separated lines and declines excessive corrections. An unsuccessful detection preserves the current crop and perspective.
- Drag a single guide corner farther when correcting steep perspective. The crop fits inside the usable part of the source image. Guide handles keep their offset from the pointer during a drag.

## 1.0.2-beta.1 — 2026-11-25

- Start a selected photo's RAW preview while Gallery thumbnails are being generated. One thumbnail worker can run alongside interactive development.
- Resolve linked originals, camera profiles and LUTs for the selected render graphs when starting an export. Cross-photo Readers and render-only effects are included.
- Index existing original paths once per multi-file import so duplicate checks scale to larger galleries.

## 1.0.1 — 2026-11-24

RAW Photo Editor is a lightweight, open-source RAW photo editor that runs offline inside Blender. This release includes non-destructive RAW development, crop and perspective correction, Spektrafilm Lite and full-resolution batch export for Windows x64 and Apple Silicon.

Install `raw_photo_editor-1.0.1.zip` from **Preferences → Get Extensions → Install from Disk** in Blender 5.3 or later. If upgrading from the old `blender_raw` extension, disable it before enabling `raw_photo_editor`.

- Read official OFX help and factory look menus from the installed plug-in. Saved recipes keep their control IDs and numeric mappings.
- Windows RAW decoding uses LLVM OpenMP. Apple Silicon uses the existing decoder.
- Removed four bundled DCP files. Current automatic calibration and user-selected DCP, ICC and LUT inputs are still available. Older recipes that specifically used a bundled DCP can select the original profile externally.
- Added the CIE standard-observer credit and license, and a source distribution for public release.

## 1.0.0 — 2026-11-24

First stable release of Blender Photo Editor for Blender 5.3 on Windows x64 and Apple Silicon.

- **Crop & Perspective** adds free cropping, six fixed aspect ratios, an original-ratio lock and a landscape/portrait toggle. Rotate to straighten and use **Fit** to keep the largest frame inside the image.
- **Four Guides** uses two horizontal and two vertical references to correct the entire photograph. **Auto Detect** finds candidate lines and lets you refine them by hand.
- **Photo Gallery** keeps an independent compositor stack for each photo. The stable toolset includes RAW development, Spektrafilm Lite, optional official OFX processing, copying adjustments, and full-resolution batch export.
- **Upgrading** from the crop demo or 0.20.3 uses the same extension ID and photo-scene format. For projects made in 0.18.x or earlier, start a new Photo Workspace and reimport the originals.
