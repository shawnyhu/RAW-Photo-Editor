# RAW Photo Editor beta.8 · Mac 黑位修复验收

请在独立 Blender 5.3 配置中离线安装本包，测试实际安装的 `bl_ext.user_default.raw_photo_editor`。不要覆盖个人配置。Windows CPU、GPU FULL 和 GPU AUTO 已验收，Mac 实际 Metal 仍需运行后才能声明通过。

beta.7 把胶片/颗粒产生的负RGB最小值作为自动黑点，将原本的0抬成灰。beta.8 的自动黑白点分别钳制到0–1；继续共用RGB范围，保留Alpha、Auto开关和逐照片适配。手动负值/HDR端点保持。旧节点组原位迁移，不要求用户重建卡片。

从 [GitHub beta.8](https://github.com/shawnyhu/RAW-Photo-Editor/releases/tag/v1.0.2-beta.8) 下载完整包，核对SHA256SUMS.txt，解压源码。先读AGENTS.md和development-log最新条目。

```sh
BLENDER=/Applications/Blender.app/Contents/MacOS/Blender
export BLENDER_USER_RESOURCES="$PWD/beta8-profile"
"$BLENDER" -b --factory-startup --offline-mode --command extension install-file --repo user_default --enable /absolute/path/raw_photo_editor-1.0.2-beta.8.zip
# 在source ZIP的仓库根目录：
"$BLENDER" -b --offline-mode --python-exit-code 1 --python tests/auto_level.py -- --installed
"$BLENDER" -b --offline-mode --python-exit-code 1 --python tests/auto_level_black_floor.py -- --installed
"$BLENDER" -b --offline-mode --python-exit-code 1 --python tests/auto_level_colorspace.py -- --installed
"$BLENDER" --offline-mode --python-exit-code 1 --python tests/auto_level_black_floor.py -- --installed --gpu
export PHOTO_UI_TEST_SCREENSHOTS="$PWD/beta8-window-evidence"
"$BLENDER" --offline-mode --enable-event-simulate -p 40 40 1200 850 --python-exit-code 1 --python tests/auto_level_ui.py -- --installed
```

GPU命令必须在有Metal权限的桌面会话执行。保留失败日志和重试来源，不把CPU回退记为GPU通过。UI脚本包含实际Viewer黑位为0断言、负尾部直方图标记、输入更新、拖动、Auto开关、取消和Undo。Retina若改变按钮坐标，只调整点击坐标，保留数值断言。`tests/fixtures/auto_level_beta7.blend` 由公开beta.7安装版生成，内含纯合成图；测试必须直接打开它，证明第一次渲染前已经迁移，手动动画仍保留。

若有本机真实RAW，可设 `PHOTO_LEVELS_RAW=/absolute/path/photo.cr3`，运行 `tests/auto_level_photo.py -- --installed --output /absolute/path/private-evidence`。它按Portra800/Vision2383、Grain、CameraDiffusion生成近似可见配方，检查旧公式抬灰与新原生输出；照片和衍生图只留本地，不发布到公共仓库。额外对两张照片复制Auto并导出，验证按各自输入调整。

交回macOS/Blender/GPU型号、ZIP和dylib摘要、各阶段PASS/FAIL、日志和必要截图。未实际运行的项目明确标记待测。无需推送或发布。若尚未完成beta.6黑白胶片验收，可继续其独立交接说明；beta.8未修改胶片库、材料和配方。
