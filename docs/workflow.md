# Photo workspace guide

## Originals and adjustments

Choose **Open Photo Workspace** and import originals into **Photo Gallery**. Selecting a thumbnail activates that photograph’s scene. The default stack contains **Raw Reader** and **Raw Developer**. Click **Compositor** beneath the effect list to inspect the selected effect’s node group.

New photos use LibRaw. Choose **Custom DCP** to load your own camera profile. Apple Silicon also offers **Apple RAW (Core Image)**. Use the Developer’s camera input, white balance and metering controls for each photo. **Proxy**, **Half** and **Full** set the interactive preview resolution.

In **Raw Developer**, **Camera & Look** selects the camera input and look. **White Balance** includes the picker, temperature and tint. Under **Auto Exposure**, **Exposure** applies manual compensation in stops. Leave **As Shot** on to use the recorded exposure with that compensation, or turn it off to start with **Spot**. Use the meter picker to place the spot, or choose a different metering mode.

In the Photo Workspace image viewer, press **N**, open **Editor**, and choose **Black**, **Dark Gray** or **Light Gray** under **Canvas Background**. Black is the default. The choice follows the workspace when switching photos and is saved with the project.

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

**Spektrafilm Lite** uses the bundled film engine. Import a `.sfpreset` or select a film and print stock. **Spektrafilm OFX** runs the official plugin in a separate process and requires a complete installation of that plugin. Use **Locate Spektrafilm OFX Folder** if Blender cannot find it. The **?** beside a control reads its help from the installed plugin. **Built-in Looks** opens that installation’s preset menu. [Official OFX download](https://spektrafilm.114c.de/)

Open **Add Scene Effect → Adjust** to add a card for **Brightness/Contrast**, **Color Balance**, **Color Correction**, **Exposure**, **Gamma**, **Hue Correct**, **Hue/Saturation/Value**, **RGB Curves** or **Tonemap**. Edit it in the effect stack. The two Spektrafilm entries form their own group in the add menu.

Add **Custom Compositor** for masks, blends and additional image inputs. Effects can be reordered and enabled separately for preview and export. Each photo keeps its own display settings.

## Copy, save and export

Right-click a source thumbnail and choose **Copy Adjustments**. Select the destination photos and choose **Paste Adjustments**. Each destination keeps its original. One Undo restores the batch.

The export dialog applies its format, quality and channel settings to the selection. **Follow Scene** uses each photo’s view settings. Export workers decode full-resolution originals independently of preview caches.

Keep linked originals available or enable **Embed Originals** when portability matters. **Pack Cache Images** controls whether decoded previews travel inside the project. To revisit the previous single-tree format used in 0.18.x and earlier, use its matching historical release.
