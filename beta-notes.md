在 Blender 5.3 的 **Preferences → Get Extensions → Install from Disk** 中选择 `raw_photo_editor-1.0.2-beta.2.zip`。安装包支持 Apple Silicon Mac 和 Windows x64，包含上一测试版的大图库导入与导出修复。

在 **Crop & Perspective** 里选择 **Vertical**、**Horizontal** 或 **Both**，然后点 **Auto Detect** 或 **Edit Guides**。前两种分别使用青色竖线、橙色横线；需要同时校正建筑的两个方向时选 **Both**。识别依据不足或变形过大时，当前构图会保留。

手动调整可以单独大幅移动一个角。按 **Enter** 应用，按 **Esc** 取消。打开旧工程时保留已保存的透视构图。

The Crop & Perspective node now offers independent vertical and horizontal guides, conservative automatic detection and a wider range of manual corner movement. Choose **Both** for a four-guide correction.

[Source and development log](https://github.com/shawnyhu/RAW-Photo-Editor/blob/88bd66bea6359bfeac936df6ea4319598fbc787b/docs/development-log.md) · [Blender verification](https://github.com/shawnyhu/RAW-Photo-Editor/actions/runs/45735328785)
