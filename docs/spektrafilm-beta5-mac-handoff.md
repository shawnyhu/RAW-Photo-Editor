# Spektrafilm Lite beta.5 · Mac 验收交接 prompt

将下面的内容交给 Apple Silicon Mac 上的 Codex，并提供本次安装包与对应源码包。手动试用可直接在 Blender 5.3 的 Preferences → Get Extensions → Install from Disk 安装 ZIP。

---

继续验收 RAW Photo Editor 1.0.2-beta.5 的 Spektrafilm Lite。请实际运行测试、保留日志，并据结果报告。先读取源码根目录 AGENTS.md，使用这次交付的安装包和对应源码。现阶段无需发布 GitHub Release。

本次安装包 SHA-256：`178331fa74af838e1eda53d1c8f8896cd957a8ac97afe7f7295472aaee5e309d`。

包内应有 56 款胶片、12 款印片材料，以及 FUJI Pro 400H、FUJI Provia100f、KODAK Ektar 100、KODAK Portra 800 四份完整预设。Apple Silicon dylib SHA-256：`19a700d2c9e3a47fa07fd3de22e09083fc15e30a8496a69aa08ab16ae87b7d30`。原生构建已完成 arm64、签名、来源数据和四款 Dyeworks 的全部 DIR 分组检查。Mac 同机像素验收尚待执行。

请在源码根目录执行以下流程，使用同一台 Mac、同一 GPU、同一 Blender 5.3.0 和脚本中的固定输入。先记录 `uname -m`、`sw_vers`、Blender 版本和 `system_profiler SPDisplaysDataType`。使用全新的临时配置，保留用户现有 Blender 配置和个人预设。

1. 核对安装包摘要，检查包内 dylib 的 arm64 架构与 `codesign --verify --strict`。确认运行时导入的是已安装的 `bl_ext.user_default.raw_photo_editor`。测试过程中不可改为源码注册。
2. 从官方 beta.4 下载地址取得旧包并核对摘要。在独立 beta.4 配置中离线安装，用 `record` 录制 160 组 Metal 像素。必须实际渲染；不可使用 `record-if-available`，GPU 不可用时记录失败原因。
3. 在另一个全新配置中离线安装最终 beta.5，使用 `compare` 对刚生成的 beta.4 基准逐像素断言相等。脚本还会验证 512 组新增配对输出有限且灰阶非平坦。保留旧基准文件，禁止用 beta.5 重录覆盖。
4. 运行新增材料校准、全部 DIR 分组、保存重开、旧负片/反转片切换、完整预设和 Live Preview 测试。最后执行原生安装及真实窗口中的预设卡片点击测试。
5. 如窗口测试因 Mac 缩放或坐标差异失败，查看实际布局后调整测试坐标，或用真实交互验证并保存证据。检查四份内置预设的完整参数恢复、Live Preview 开/关保持、同名个人副本可管理，以及在第二张卡片活动时对第一张卡片应用预设。

以下是可直接调整安装包路径后执行的 Bash 命令。安装包名称相同的旧中间产物不能混用。

```bash
set -euo pipefail
blender=/Applications/Blender.app/Contents/MacOS/Blender
installer=/absolute/path/to/raw_photo_editor-1.0.2-beta.5.zip
validation=$(mktemp -d "${TMPDIR:-/tmp}/raw-photo-beta5.XXXXXX")
mkdir -p "$validation/beta4-profile" "$validation/final-profile"
printf '%s  %s\n' 178331fa74af838e1eda53d1c8f8896cd957a8ac97afe7f7295472aaee5e309d "$installer" | shasum -a 256 -c -
uname -m > "$validation/architecture.txt"
sw_vers > "$validation/macos.txt"
system_profiler SPDisplaysDataType > "$validation/gpu.txt"
"$blender" --version > "$validation/blender.txt"

previous="$validation/raw_photo_editor-1.0.2-beta.4.zip"
curl --fail --location --retry 3 -o "$previous" \
  https://github.com/shawnyhu/RAW-Photo-Editor/releases/download/v1.0.2-beta.4/raw_photo_editor-1.0.2-beta.4.zip
printf '%s  %s\n' d7704cb93d17570b729f85c927fdf5bf5effa76b28d6f0b6252d3ea654eec55f "$previous" | shasum -a 256 -c -

export BLENDER_USER_RESOURCES="$validation/beta4-profile"
"$blender" -b --factory-startup --offline-mode --command extension install-file --repo user_default --enable "$previous" 2>&1 | tee "$validation/beta4-install.log"
"$blender" -b --offline-mode --python-exit-code 1 --python tests/spektrafilm_stock_pixels.py -- record "$validation/beta4-metal-160.npz" 2>&1 | tee "$validation/beta4-record.log"
shasum -a 256 "$validation/beta4-metal-160.npz" > "$validation/baseline.sha256"

export BLENDER_USER_RESOURCES="$validation/final-profile"
"$blender" -b --factory-startup --offline-mode --command extension install-file --repo user_default --enable "$installer" 2>&1 | tee "$validation/final-install.log"
"$blender" -b --offline-mode --python-exit-code 1 --python tests/spektrafilm_stock_pixels.py -- compare "$validation/beta4-metal-160.npz" 2>&1 | tee "$validation/stock-pixels.log"
shasum -a 256 -c "$validation/baseline.sha256"
"$blender" -b --offline-mode --python-exit-code 1 --python tests/spektrafilm_added_calibration.py -- --installed --pixels 2>&1 | tee "$validation/added-calibration.log"
"$blender" -b --offline-mode --python-exit-code 1 --python tests/spektrafilm_calibration.py 2>&1 | tee "$validation/original-calibration.log"
"$blender" -b --offline-mode --python-exit-code 1 --python tests/spektrafilm_user_settings.py 2>&1 | tee "$validation/settings.log"
for stage in write use; do
  "$blender" -b --offline-mode --python-exit-code 1 --python tests/spektrafilm_user_preset_flow.py -- "$stage" "$validation/presets" 2>&1 | tee "$validation/presets-$stage.log"
done
"$blender" -b --offline-mode --python-exit-code 1 --python tests/native_install_smoke.py 2>&1 | tee "$validation/install-smoke.log"
"$blender" --offline-mode --enable-event-simulate --window-geometry 50 50 1600 1000 --python-exit-code 1 --python tests/spektrafilm_user_presets_ui.py -- --installed 2>&1 | tee "$validation/presets-ui.log"
printf 'Validation evidence: %s\n' "$validation"
```

验收报告请分别列出：安装包和原生库摘要、Mac/GPU/Blender 环境、160 组逐像素结果、512 组新增配对结果、校准和保存重开、四份预设与卡片交互。每项明确 PASS、FAIL 或未执行，并附日志位置。出现像素差异时保留首个失败的胶片/印片 ID、最大差值和基准文件，再定位原因。将结果补到开发日志，并把报告和日志打包交回。通过实际验收后再决定正式发布。
