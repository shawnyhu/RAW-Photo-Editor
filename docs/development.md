# 在新电脑上接手开发

本文以公开版本 [v1.0.1](https://github.com/shawnyhu/RAW-Photo-Editor/releases/tag/v1.0.1) 为基线。继续开发请从公开仓库的 `main` 开分支；复现这次发布可检出标签 `v1.0.1`。公开仓库有独立的 Git 历史，早期迭代的技术背景见[开发日志](development-log.md)。界面操作见[用户指南](workflow.md)。

## 第一次构建和离线安装

需要 Git 和 Blender 5.3。发布测试使用 Blender 5.3.0。支持 Windows x64 和 macOS 14 及以上的 Apple Silicon。以下命令从同一个终端执行，先克隆仓库：

```sh
git clone https://github.com/shawnyhu/RAW-Photo-Editor.git
cd RAW-Photo-Editor
git switch -c codex/my-change
```

源码目录为 `blender_raw/`。干净克隆包含[清单](../blender_raw/blender_manifest.toml)列出的四个 wheel，可直接离线打包。要改 Windows 解码器，再参考[专用构建流程](../.github/workflows/prepare-decoder.yml)。官方 Spektrafilm OFX 只在开发或使用 OFX 功能时需要。

**Windows · PowerShell**。在仓库根目录运行。按实际安装位置调整第一行。使用临时 Blender 配置，以便确认载入的是刚构建的 ZIP。

```powershell
$blender = 'C:\Program Files\Blender Foundation\Blender 5.3\blender.exe'
$session = New-Item -ItemType Directory -Path (Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString()))
$env:BLENDER_USER_RESOURCES = (New-Item -ItemType Directory -Path (Join-Path $session.FullName 'profile')).FullName
$zip = Join-Path $session.FullName 'raw_photo_editor.zip'
& $blender -b --factory-startup --offline-mode --command extension build --source-dir blender_raw --output-filepath $zip
& $blender -b --factory-startup --offline-mode --command extension install-file --repo user_default --enable $zip
& $blender -b --offline-mode --python-exit-code 1 --python tests/native_install_smoke.py
& $blender -b --offline-mode --python-exit-code 1 --python tests/batch_export_format.py -- --installed
& $blender -b --offline-mode --python-exit-code 1 --python tests/crop_perspective.py
```

**macOS · Terminal**。在仓库根目录运行。Blender 安装于其他位置时调整第一行。

```sh
blender=/Applications/Blender.app/Contents/MacOS/Blender
session_dir=$(mktemp -d)
export BLENDER_USER_RESOURCES="$session_dir/profile"
mkdir -p "$BLENDER_USER_RESOURCES"
zip="$session_dir/raw_photo_editor.zip"
"$blender" -b --factory-startup --offline-mode --command extension build --source-dir blender_raw --output-filepath "$zip"
"$blender" -b --factory-startup --offline-mode --command extension install-file --repo user_default --enable "$zip"
"$blender" -b --offline-mode --python-exit-code 1 --python tests/native_install_smoke.py
"$blender" -b --offline-mode --python-exit-code 1 --python tests/batch_export_format.py -- --installed
"$blender" -b --offline-mode --python-exit-code 1 --python tests/crop_perspective.py
```

三项测试分别检查已安装扩展与 EXR 像素、整批 JPEG 编码和裁切几何及输出。每条命令应以状态码 0 退出；第一项会打印 `PASS installed ZIP`。安装与测试要沿用同一配置。安装后的测试需要已启用的扩展，所以测试命令省略了 `--factory-startup`。修改源码后，重新构建并在新的临时配置中安装即可验证新代码。

如果正在修改源码，希望先快速验证照片绑定，可直接加载 checkout。在上述 Windows 终端执行：

```powershell
& $blender -b --factory-startup --offline-mode --python-exit-code 1 --python tests/native_source_binding.py
```

macOS 将命令开头的 `& $blender` 换成 `"$blender"`。这条命令使用出厂配置；脚本自行注册源码中的扩展。测试发布包时再走上面的安装路径。

## 从症状定位代码

| 要修改或排查的行为 | 先看这里 | 如何验证 |
| --- | --- | --- |
| 切图、打开照片、增删或排序效果 | [compositor.py](../blender_raw/compositor.py) 的 `ensure_photo_scene` 创建照片 Scene；[scene_pipeline.py](../blender_raw/scene_pipeline.py) 的 `effects` 和 `upstream` 遍历启用的效果及嵌套节点。 | [原生 Scene](../tests/native_photo_scenes.py)、[源图绑定](../tests/native_source_binding.py)。切图残影需在真实窗口做 A → B → A，再读取 [Viewer 像素](../tests/native_photo_switch_ui.py)。 |
| 批量粘贴后串图，或修改副本影响原图 | [adjustments.py](../blender_raw/adjustments.py) 复制 Reader 之后的效果，保留目标原片；[native_effects.py](../blender_raw/native_effects.py) 处理可变节点组的独立副本。 | [批量调整回归](../tests/native_adjustments.py)检查来源、目标和保存重开。涉及 Undo/Redo 时另跑真实窗口模式。 |
| 预览正确，导出分辨率或颜色异常 | [catalog.py](../blender_raw/catalog.py) 的 `request` 和 `_full_request` 管理预览及全尺寸缓存。[jobs.py](../blender_raw/jobs.py) 的 `start_export` 冻结批次；[worker.py](../blender_raw/worker.py) 的 `export_batch` 读取快照、渲染临时文件并写入目标。 | 先跑上面的批量编码测试，再按 [worker 测试](../tests/native_worker_export.py)传入一张可用的真实 RAW。检查最终文件尺寸、像素及各照片自己的设置。 |
| 裁切边缘、透视、输出尺寸或拖拽偏移 | [crop_geometry.py](../blender_raw/crop_geometry.py) 求坐标变换，[crop_node.py](../blender_raw/crop_node.py) 的 `terminal_dimensions` 按当前预览或导出尺寸计算最终画幅。[crop_editor.py](../blender_raw/crop_editor.py) 处理窗口交互，[crop_detect.py](../blender_raw/crop_detect.py) 处理自动找线。 | 像素与几何用上面的裁切测试。改手柄后另跑[窗口测试](../tests/crop_perspective_ui.py)；真实鼠标事件和完成标记的命令见[发布工作流](../.github/workflows/release.yml)。 |
| 胶片预览、预乘 Alpha、OFX 加载 | [film_bake.py](../blender_raw/film_bake.py) 的 `prepared` 取得渲染输入。[film_backend.py](../blender_raw/film_backend.py) 分派到捆绑的 Lite 或外部 OFX；[OFX 适配器](../blender_raw/spektrafilm_ofx_engine.py) 检查插件版本并启动独立 host。 | 普通安装 smoke 可验证扩展和依赖。OFX 像素、Alpha 和后台导出另见[原生 OFX 测试](../tests/native_ofx_pipeline.py)，需要完整官方插件。 |
| Auto Level 自动取点、手动直方图、复制后适配 | [levels_node.py](../blender_raw/levels_node.py) 构建原生全图统计与映射；[levels_ui.py](../blender_raw/levels_ui.py) 只抓取输入 scopes、处理拖动与开关。 | [实际像素与复制](../tests/auto_level.py)、[黑白位和旧图迁移](../tests/auto_level_black_floor.py)、[输入色域](../tests/auto_level_colorspace.py)、[GPU AUTO Viewer 和真实拖动](../tests/auto_level_ui.py)。Mac 见[beta.8 交接](auto-level-beta8-mac-handoff.md)。 |
| 从 ZIP 首装失败，直接运行源码却正常 | [扩展注册](../blender_raw/__init__.py)和[原生面板](../blender_raw/native_effects.py)涉及 Blender 的受限注册上下文与注销顺序。 | [Extension Manager 测试](../tests/native_extension_install_ui.py)有 fresh、upgrade 和 restart 等模式；用独立配置和真实窗口复现。 |

**缓存与原片。** `Embed Originals` 随项目保存原始文件，`Pack Cache Images` 随项目保存已解码的预览。排查换机问题时，先确认原片是否可读，以及外部 DCP、ICC、LUT 是否仍能定位。导出需要匹配的全尺寸数据；若只有低分辨率预览，worker 需要重新读取原片。

**选择测试。** 仓库保留了早期实现的测试。例如 [packed_decode_cache.py](../tests/packed_decode_cache.py)、[cache_storage_policy.py](../tests/cache_storage_policy.py) 和 [spektrafilm_switch.py](../tests/spektrafilm_switch.py) 仍使用旧接口 `compositor.ensure_tree` 或 `photo.tree`。它们记录了需要保留的行为。要恢复这部分自动化覆盖，应先把用例移植到 `photo.scene` 和原生效果栈。真实 RAW、真实 GPU、外部 OFX、窗口事件各有不同的前置条件，详见对应测试脚本和工作流。

## 版本与交接

- 查看公开发布的测试范围和结果：[v1.0.1 构建记录](https://github.com/shawnyhu/RAW-Photo-Editor/actions/runs/45360386980)。这一版在 Apple Silicon 构建 ZIP，Windows x64 下载并安装同一个 ZIP。两个平台验证后发布安装包、对应源码和各自的 SHA-256。
- 更新版本时同步[扩展清单](../blender_raw/blender_manifest.toml)与[变更记录](../CHANGELOG.md)。发布工作流只在满足触发条件时上传 Release：推送到 `main`、包含 manifest 变更、提交标题精确匹配 `Release v<version>`，且平台检查通过。手动运行该工作流可以验证构建。
- 交接一次实质改动时，在[开发日志](development-log.md)补上版本或提交、复现条件、决定及原因、相关实现、验证环境和未验证项。将可重复的测试命令或公开 CI 链接放入同一条记录，下一位开发者就能判断从哪里继续。
