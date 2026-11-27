# RAW Photo Editor 1.0.2-beta.7 · Mac 验收交接

请在这台 Apple Silicon Mac 验收 beta.7 的 Auto Level。保留已有 Blender 配置和项目，使用独立配置离线安装。代码已在 Windows Blender 5.3.0、RTX 4090 上通过 CPU 导出、GPU FULL 和真实 GPU AUTO Viewer 像素验收；本次交付尚未声明 Mac 实际 Metal 通过。

从 GitHub [v1.0.2-beta.7](https://github.com/shawnyhu/RAW-Photo-Editor/releases/tag/v1.0.2-beta.7) 下载 complete ZIP，解压并核对 SHA256SUMS.txt。其中 installer 是 Blender 扩展；source ZIP 解压后包含 AGENTS.md、实现、测试与开发日志。先读这三个文件，再开始测试。不要只重新构建代码或导入工作目录来替代安装版验收。

核心需求：Auto Level 是持续开关，默认开启。每张卡片通过原生节点图测量当前输入的共有 RGB 极值；复制到不同照片、上游改变、保存重开和全尺寸导出都自动适配。透明像素不参与极值，Alpha 保持，恒定和全透明图自动旁路。输入/输出 histogram 使用线性工作色域。Edit Histogram 中拖黑白点会转手动并保留另一端；Auto 再开启恢复动态计算。Esc 取消，Done/Enter 接受，Undo 可撤销。

在终端设定实际路径后执行，保留每条命令的退出码与完整日志：

```sh
BLENDER=/Applications/Blender.app/Contents/MacOS/Blender
export BLENDER_USER_RESOURCES="$PWD/beta7-test-profile"
"$BLENDER" -b --factory-startup --offline-mode --command extension install-file --repo user_default --enable /absolute/path/raw_photo_editor-1.0.2-beta.7.zip
# 以下从 source ZIP 解压的仓库根目录执行。
"$BLENDER" -b --offline-mode --python-exit-code 1 --python tests/native_install_smoke.py
"$BLENDER" -b --offline-mode --python-exit-code 1 --python tests/adjust_effect_cards.py -- --installed
"$BLENDER" -b --offline-mode --python-exit-code 1 --python tests/auto_level.py -- --installed
"$BLENDER" -b --offline-mode --python-exit-code 1 --python tests/auto_level_colorspace.py -- --installed
"$BLENDER" --offline-mode --python-exit-code 1 --python tests/auto_level.py -- --installed --gpu
export PHOTO_UI_TEST_SCREENSHOTS="$PWD/beta7-window-evidence"
"$BLENDER" --offline-mode --enable-event-simulate -p 40 40 1200 850 --python-exit-code 1 --python tests/auto_level_ui.py -- --installed
```

最后两个命令必须从有 Metal 访问权限的桌面会话运行。若受限进程不能访问 GPU，记录失败，然后从已授权桌面会话重跑；不可把跳过或 CPU 回退记为 GPU 通过。`auto_level_ui.py` 会读取实际 GPU AUTO Viewer 像素、模拟黑白点拖动、切换 Auto、取消/Undo，并检查原位输入变化后 histogram 更新、空闲时停止抓取。若 Retina 或主题使卡片按钮坐标不同，只调整测试中的点击坐标；保留像素、数值和目标卡片断言。

另用两张亮度范围明显不同的真实照片，复制 Auto 卡片，改变前序曝光，分别导出 EXR。检查两个自动范围独立、后序效果不影响本卡片测量；手动关闭 Auto 后范围保持。截图记录卡片、编辑直方图和输出效果，报告 Blender/macOS/GPU、安装包及 dylib SHA-256、每项 PASS/FAIL 与日志路径。

beta.7 没有修改 beta.6 的胶片原生库、数据或推荐配方。若这台 Mac 尚未验收 beta.6 的黑白胶片，继续执行源码中的 `docs/spektrafilm-beta6-mac-handoff.md`；把胶片与 Auto Level 的测试结果分别记录。未实际运行的项目明确标记待测。不要发布 Release 或推送远端；先把报告、截图、日志和任何补丁打包交回。
