# Changelog

## 1.0.2-beta.7 — 2026-11-27

- Added **Adjust → Auto Level**. Auto is a persistent switch: the native compositor recomputes a shared RGB black/white range from each card's current input. Copies adapt to their destination photographs, and final exports analyze their full-resolution input.
- The card displays input and output histograms. **Edit Histogram** provides draggable black/white points over the input distribution. Dragging switches to Manual and keeps the other endpoint; turn Auto back on to resume automatic adjustment. Mode and manual values support copying, Undo and saving with the project.
- Transparent pixels do not determine the automatic range. Alpha is preserved, and uniform or fully transparent inputs pass through unchanged in Auto. Scopes show linear RGB from the current preview; automatic processing measures every pixel of the actual render input.

## 1.0.2-beta.6 — 2026-11-27

- Film and Print now open brand-column menus with readable stock names and the current selection marked. Print choices are filtered to supported materials for the selected film.
- Selecting any film loads its complete recommended starting settings: print material or direct slide scanning, development calibration, DIR, grain and halation parameters. Diffusion and scanner settings reset to neutral defaults. Grain and halation start disabled; their physical stock settings are ready when enabled. The reset icon beside **Film Defaults** reapplies the selected film's starting settings. Live Preview and upstream RAW development are preserved.
- Added eight black-and-white films and two black-and-white print materials, bringing Lite to **64 films and 14 print materials**. Black-and-white uses single-channel exposure and silver density, with the source's reference development curve and grain layers. Tri-X 7266 uses direct reversal scanning. Tri-X 400 / D-76 and Ilford Multigrade RC Deluxe grade 2 retain their source's experimental status; the latter models a fixed paper grade and normalized black-and-white printing.
- DIR calibration is internal to the film model and no longer appears as an editable Lite panel. Existing projects and saved personal looks retain their stored settings; the new defaults apply when a film is explicitly selected or restored.
- Restored the native **RGB to Raw**, **Film Format** and **Film Push / Pull Stops** controls. They retain imported settings, participate in personal presets, and persist with the project.
- Removed the four bundled creative presets. **Presets** is now the user's personal library; existing saved copies remain available for applying, renaming and deleting.

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
