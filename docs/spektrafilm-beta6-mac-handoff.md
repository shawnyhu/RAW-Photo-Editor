# Mac Codex 验收交接：1.0.2-beta.6

在 Apple Silicon Mac 上验收 RAW Photo Editor 1.0.2-beta.6。先读解压源码中的 AGENTS.md、docs/development.md 及开发日志最新条目。保留已有用户配置和照片，用独立 Blender 配置离线安装。全程不安装 OFX。不要把 Windows 基准当成 Metal 基准，不要覆盖任何已有基准。

本版改动：64 款胶片、14 款印片材料；选择 Film 自动载入推荐 Print/模式/全部参数；个人预设保留完整自定义效果；移除四份内置效果预设；DIR 作为内部模型数据；Film/Print 改为按品牌分列的菜单。黑白新增 8 款 Film、2 款 Print，其中 Tri-X 400 / D-76 与 Ilford Multigrade RC Deluxe grade 2 带实验标记。

## 下载与环境

从用户提供的 GitHub beta.6 交付链接下载安装包、源码包和 SHA-256 清单，先核对摘要。记录 macOS、芯片/GPU、Blender 版本、原生 dylib 摘要、`file` / `lipo -archs` / `codesign --verify --strict --verbose=2` 结果。使用 Blender 5.3.0。源码解压目录作为以下命令的工作目录：

```sh
blender=/Applications/Blender.app/Contents/MacOS/Blender
test_root=$(mktemp -d)
mkdir -p "$test_root/beta5-profile" "$test_root/beta6-profile" "$test_root/reports"
```

从同一仓库的 v1.0.2-beta.5 release 下载旧安装包。beta.5 SHA-256 必须为 `178331fa74af838e1eda53d1c8f8896cd957a8ac97afe7f7295472aaee5e309d`。设 `previous` 为该 ZIP 的绝对路径、`package` 为最终 beta.6 ZIP 的绝对路径。

## 同机 Metal 回归

先在 beta.5 独立配置录制所有 56×12 配对的像素、完整配方和旧节点保存文件。仅在此步骤运行 record：

```sh
BLENDER_USER_RESOURCES="$test_root/beta5-profile" "$blender" -b --factory-startup --offline-mode --command extension install-file --repo user_default --enable "$previous"
BLENDER_USER_RESOURCES="$test_root/beta5-profile" "$blender" -b --offline-mode --python-exit-code 1 --python tests/spektrafilm_color_compatibility.py -- record "$test_root/beta5-672-reference.npz"
BLENDER_USER_RESOURCES="$test_root/beta5-profile" "$blender" -b --offline-mode --python-exit-code 1 --python tests/spektrafilm_effect_compatibility.py -- record "$test_root/beta5-168-effects-reference.npz"
shasum -a 256 "$test_root/beta5-672-reference.npz" "$test_root/beta5-672-reference.blend"
chmod a-w "$test_root/beta5-672-reference.npz" "$test_root/beta5-672-reference.blend"
chmod a-w "$test_root/beta5-168-effects-reference.npz"
```

若进程无法获得 Metal，保留失败日志，使用用户启动的桌面会话或可访问 GPU 的执行方式重试。构建或安装成功不能替代像素验收。不可使用 `record-if-available` 的跳过结果充当 PASS。

然后安装最终 beta.6；确认日志导入路径为 `bl_ext.user_default.raw_photo_editor` 的安装目录：

```sh
export BLENDER_USER_RESOURCES="$test_root/beta6-profile"
"$blender" -b --factory-startup --offline-mode --command extension install-file --repo user_default --enable "$package"
"$blender" -b --offline-mode --python-exit-code 1 --python tests/spektrafilm_color_compatibility.py -- compare "$test_root/beta5-672-reference.npz"
"$blender" -b --offline-mode --python-exit-code 1 --python tests/spektrafilm_effect_compatibility.py -- compare "$test_root/beta5-168-effects-reference.npz"
"$blender" -b --offline-mode --python-exit-code 1 --python tests/spektrafilm_stock_defaults.py -- --pixels
"$blender" -b --offline-mode --python-exit-code 1 --python tests/spektrafilm_added_calibration.py -- --pixels
"$blender" -b --offline-mode --python-exit-code 1 --python tests/spektrafilm_calibration.py
"$blender" -b --offline-mode --python-exit-code 1 --python tests/spektrafilm_user_settings.py
"$blender" -b --offline-mode --python-exit-code 1 --python tests/native_install_smoke.py
for stage in write use; do
  "$blender" -b --offline-mode --python-exit-code 1 --python tests/spektrafilm_user_preset_flow.py -- "$stage" "$test_root/personal-presets"
done
```

要求 672 组既有彩色配方逐像素完全一致，60 个旧节点（含已应用的四份旧内置预设）配方不变。新推荐设置有意改变 Film 选择行为，比较时使用冻结的旧配方。黑白验收要求所有 15 条支持的路径（7 负片×2 印片、1 反转直扫）输出有限、灰阶有效且 RGB 中性；24 组黑白颗粒渲染覆盖三个模型。所有 64 款推荐、个人编辑及 Live Preview 保存重开必须通过。

如 Mac 已有同机 beta.4 的 160 组基准，可额外运行 `tests/spektrafilm_stock_pixels.py -- compare <只读基准>`。不要用新版 record 重录该基准。

## 真实窗口

运行 `tests/spektrafilm_user_presets_ui.py -- --installed` 与 `tests/spektrafilm_stock_picker_ui.py` 中注明的窗口命令，使用 `--enable-event-simulate`。同时人工检查 1200×850 和较大窗口：

- Film / Print 显示可读型号，按品牌分列，当前选项有标记；菜单项目可达且没有截断。
- 第二张卡片活动时，点击第一张卡片的 Film 仍只更新第一张；选择 HP5 后自动切到对应黑白 Print，Print 菜单只列出支持的黑白材料。
- 切换 Kodak / Fujifilm 彩色负片、反转片及黑白片，推荐材料、模式与可见参数同步更新。切回 Film 会恢复推荐值；保存并重新应用个人预设会恢复用户效果。
- DIR 不再出现在 Lite 控件中。个人 Presets 菜单中没有那四份系统内置项，既有同名个人副本仍可管理。
- Live Preview 开关保持目标卡片的值；导出 EXR 像素正常。

截图至少包含完整 Film 菜单、黑白 Print 菜单，以及真实点击前后的两张卡片。节点值断言与事件日志一并保留；相同截图不当作前后效果证据。

## 回传

生成报告：每项 PASS / FAIL / SKIP、完整命令及退出码、设备和版本、安装包/dylib/基准摘要、导入路径。附原始日志、只读基准及 `.blend`、截图。明确区分构建验证和实际 Metal 渲染。失败时定位并报告，不把跳过归入通过。保留用户配置；不自行发布 Release 或更改既有标签。
