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

**Presets** stores your own complete looks. Adjust a node and choose **Save Current as Preset**; apply, rename or delete saved looks in this menu. The four creative looks bundled in beta.5 have been removed. Personal copies with the same names remain in your library, and older projects retain their saved settings.

Click **Film** or **Print** to open a menu grouped into brand columns. Lite includes 64 films and 14 print materials. Film choices are further grouped as color negative, slide, black-and-white negative or black-and-white reversal. The current material has a checkmark. Print offers the materials supported by the selected film.

Choosing **Film** loads a complete recommended starting point: a matching Print or direct reversal scan, development and internal DIR calibration, and physical grain and halation settings. Grain and halation initially remain off; enable them to use those parameters. Diffusion and scanner settings reset to neutral defaults. **Restore Film Defaults** reapplies this starting point. Save custom effects as a personal preset before trying other film recommendations. Applying a preset restores the complete saved look. Both actions preserve the destination's Live Preview setting and upstream RAW adjustments.

All spectral curves, development data and print calibration ship in the offline engine. DIR is managed internally by the film model. The first 56 color film and 12 color print indices remain unchanged. The eight black-and-white films use single-channel silver density; seven negative stocks can print onto Kodak 2302 or Ilford Multigrade RC Deluxe grade 2. Tri-X Reversal 7266 defaults to a direct scan. Tri-X 400 / D-76 and Ilford grade 2 are experimental source profiles; the Ilford material represents normalized black-and-white printing at fixed grade 2. Sources, licenses and model limits are in the bundled [stock changelog](../blender_raw/vendor/spektrafilm/CHANGELOG.txt).

**Spektrafilm OFX** runs the official plugin in a separate process and requires a complete installation of that plugin. Use **Locate Spektrafilm OFX Folder** if Blender cannot find it. The **?** beside a control reads its help from the installed plugin. **Built-in Looks** opens that installation’s preset menu. [Official OFX download](https://spektrafilm.114c.de/)

Open **Add Scene Effect → Adjust** to add a card for **Brightness/Contrast**, **Color Balance**, **Color Correction**, **Exposure**, **Gamma**, **Hue Correct**, **Hue/Saturation/Value**, **RGB Curves** or **Tonemap**. Edit it in the effect stack. The two Spektrafilm entries form their own group in the add menu.

Add **Custom Compositor** for masks, blends and additional image inputs. Effects can be reordered and enabled separately for preview and export. Each photo keeps its own display settings.

## Copy, save and export

Right-click a source thumbnail and choose **Copy Adjustments**. Select the destination photos and choose **Paste Adjustments**. Each destination keeps its original. One Undo restores the batch.

The export dialog applies its format, quality and channel settings to the selection. **Follow Scene** uses each photo’s view settings. Export workers decode full-resolution originals independently of preview caches.

Keep linked originals available or enable **Embed Originals** when portability matters. **Pack Cache Images** controls whether decoded previews travel inside the project. To revisit the previous single-tree format used in 0.18.x and earlier, use its matching historical release.
