# Photo workspace guide

## Originals and adjustments

Choose **Open Photo Workspace** and import originals into **Photo Gallery**. Selecting a thumbnail activates that photograph’s scene. The default stack contains **Raw Reader** and **Raw Developer**. Click **Compositor** beneath the effect list to inspect the selected effect’s node group.

New photos use LibRaw. Choose **Custom DCP** to load your own camera profile. Apple Silicon also offers **Apple RAW (Core Image)**. Use the Developer’s camera input, white balance and metering controls for each photo. **Proxy**, **Half** and **Full** set the interactive preview resolution.

In **Raw Developer**, **Camera & Look** selects the camera input and look. **White Balance** includes the picker, temperature and tint. Under **Auto Exposure**, **Exposure** applies manual compensation in stops. Leave **As Shot** on to use the recorded exposure with that compensation, or turn it off to start with **Spot**. Use the meter picker to place the spot, or choose a different metering mode.

In the Photo Workspace image viewer, press **N**, open **Editor**, and choose **Black**, **Dark Gray** or **Light Gray** under **Canvas Background**. Black is the default. The choice follows the workspace when switching photos and is saved with the project.

New Photo Workspaces start with **Overlays → Guides → Grid** off. Turn it on there to save a different choice with the project.

## Crop and perspective

Add **Crop & Perspective** near the end of the effect stack to frame the finished image.

| Control | Use |
| --- | --- |
| **Edit Crop Frame** | Drag a corner to resize or drag inside to move. Choose **Free**, **Original**, 1:1, 5:4, 4:3, 3:2, 16:9 or 2.39:1. The arrow beside the ratio swaps width and height. |
| **Rotation** and **Fit** | Straighten the image and fit the largest frame of the selected ratio. In the crop editor, use `[` and `]` for 1° steps, or hold Shift for 0.1°. |
| **Vertical**, **Horizontal**, **Both** | Choose the direction for your next edit or detection. The status above the buttons shows the correction currently applied. |
| **Edit Guides** | In **Vertical**, place the two cyan guides along upright edges, such as doorposts. In **Horizontal**, align the two amber guides with level edges, such as shelves. **Both** uses all four sides. Drag the endpoints on the original image. |
| **Auto Detect** | Look for two distinct, well-supported lines in each requested direction. If the scene only has reliable uprights, select **Vertical**. When the evidence is weak or the proposed warp is extreme, the current adjustment stays in place. |

Press **Enter** to apply or **Esc** to discard the current drag session. Each application replaces the node’s previous perspective correction. Choose **Both** to straighten both directions together. **Disable Perspective** keeps the crop controls available.

## Film and compositor effects

**Spektrafilm Lite** uses the bundled film engine. Choose a film and print stock, adjust the look, then open **Presets → Save Current as Preset** and give it a name. On any Lite node, click the name in **Presets** to apply the look. The pencil renames an entry and the × deletes it from the library. Applying a look keeps the destination node's Live Preview setting. Projects retain the applied settings even if you later delete the preset.

Presets are shared across projects and Blender versions for the current OS user. For backup or transfer to another computer, copy the **Spektrafilm Lite** folder. On macOS it is in `~/Library/Application Support/Blender/RAW Photo Editor/`. On Windows it is in `%APPDATA%\Blender Foundation\Blender\RAW Photo Editor\`.

**Presets → Built-in Presets** includes FUJI Pro 400H, FUJI Provia100f, KODAK Ektar 100 and KODAK Portra 800 on a fresh installation. Click a name to apply the complete recipe. To make a variation, edit the node and choose **Save Current as Preset**. Existing personal presets with the same name remain in the editable list above.

The **Film** and **Print** selectors offer 56 films and 12 print materials. The additional films load their stock-specific DIR, halation and grain defaults when selected. Their spectral curves, development data and paired print-filter calibration are included in the offline engine. The original 20 film and 8 print entries keep their indices. The four additional print films are Fujifilm Eterna-CP 3513DI, 3514DI, 3521XD and 3523XD. Sources, licenses and modeling limits are recorded in the bundled [stock changelog](../blender_raw/vendor/spektrafilm/CHANGELOG.txt).

**Spektrafilm OFX** runs the official plugin in a separate process and requires a complete installation of that plugin. Use **Locate Spektrafilm OFX Folder** if Blender cannot find it. The **?** beside a control reads its help from the installed plugin. **Built-in Looks** opens that installation’s preset menu. [Official OFX download](https://spektrafilm.114c.de/)

Open **Add Scene Effect → Adjust** to add a card for **Brightness/Contrast**, **Color Balance**, **Color Correction**, **Exposure**, **Gamma**, **Hue Correct**, **Hue/Saturation/Value**, **RGB Curves** or **Tonemap**. Edit it in the effect stack. The two Spektrafilm entries form their own group in the add menu.

Add **Custom Compositor** for masks, blends and additional image inputs. Effects can be reordered and enabled separately for preview and export. Each photo keeps its own display settings.

## Copy, save and export

Right-click a source thumbnail and choose **Copy Adjustments**. Select the destination photos and choose **Paste Adjustments**. Each destination keeps its original. One Undo restores the batch.

The export dialog applies its format, quality and channel settings to the selection. **Follow Scene** uses each photo’s view settings. Export workers decode full-resolution originals independently of preview caches.

Keep linked originals available or enable **Embed Originals** when portability matters. **Pack Cache Images** controls whether decoded previews travel inside the project. To revisit the previous single-tree format used in 0.18.x and earlier, use its matching historical release.
