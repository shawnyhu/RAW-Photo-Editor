# RAW Photo Editor 开发日志

这份记录从早期 Windows 移植写到首个公开版本，重点是几次改变工程结构或导致错误输出的决定。0.x 版本号来自迁入公开仓库前的开发记录。文中链接指向目前可查阅的公开实现和测试；[CHANGELOG](../CHANGELOG.md) 记录面向用户的版本变化，[开发交接](development.md) 说明如何在新电脑上构建和验证。

## 2026-11-18 至 19 · 离线安装与 Windows 移植（0.7–0.15.x）

最初的 Windows 安装包解决了离线 RAW 显影。加入 Apple Silicon 后，一个 ZIP 需要为两个平台携带对应 Python wheel 和原生库。胶片渲染在 Mac 使用 Metal，在 Windows 使用 Vulkan。移植时有两个故障适合作为以后更换依赖或后端时的对照。

- **Nikon Z8 HE 的缓存身份。** Windows 临时目录可能同时出现 8.3 短路径和长路径。Reader 和 Developer 请求同一份 EXR，却可能拿到两个 Blender Image 数据块。0.15.5 按请求键和解析后的绝对路径识别同一份缓存。若再出现重复解码或图像绑定错误，保留冷热缓存与两种路径的对照，并比较同设置下的线性 RGB。参见[缓存与源文件处理](../blender_raw/catalog.py)。
- **Scanner 开关影响跨平台色彩验证。** 不开 Scanner 的参考图可以通过，但实际启用 Scanner 的印片结果仍出现黑白点偏差。0.15.6 修正了 Vulkan 端的黑白点转换与印片标定。以后改胶片后端，要用同一输入、胶片和印片分别比较 Scanner 开关前后的线性 RGB 与 Alpha。参见[胶片引擎来源与本地改动](../blender_raw/vendor/spektrafilm/NOTICE.md)。

随后，原片、解码预览与 Gallery 缩略图逐步分开管理。预览可以从缓存恢复；用户选择嵌入原片时，工程才会携带重建原始显影所需的文件。在新电脑验证工程可携带性时，分别测试链接原片和嵌入原片。[用户指南](workflow.md)

## 2026-11-19 至 20 · 每张照片拥有独立 Scene（0.16–0.19.6）

0.18 的工作流从照片节点树推算卡片顺序，批量复制调整已经需要保护目标原片和自定义连线。0.19 转向 Blender 5.3 的原生 Compositor & Effects。照片通过 `photo.scene` 持有 Scene，`scene.compositor_effects` 决定效果顺序。Raw Reader 与 Raw Developer 是初始栈，复杂分支和遮罩放入 Custom Compositor。预览与导出各自遵守效果开关。0.18.x 及更早工程使用单树模型，继续处理旧工程用其匹配版本；切换到新模型可新建 Photo Workspace 并重新导入原片。[照片场景](../blender_raw/compositor.py) · [效果遍历](../blender_raw/scene_pipeline.py)

这次迁移留下了三个有明确复现步骤的回归：

| 症状 | 定位与修复 | 再验证 |
| --- | --- | --- |
| 从偏好设置启用扩展失败，第二次启用报 `BR_Photo` 重复注册 | 首次注册期间创建辅助 Scene，遇到 Extension Manager 受限的 Blender 上下文后中断，已注册的类型留在进程中。0.19.2 将创建 Scene 延至注册完成后的 timer。 | 用新配置，通过真实窗口首装和再次启用。[安装与升级测试](../tests/native_extension_install_ui.py) |
| 两张同尺寸照片间 A → B → A，Gallery 已选中 A，Viewer 还显示 B | 0.19.6 在实际切换 Scene 后重设活动效果以触发合成，同时保留每张照片的卡片选择。只检查当前 Scene 会漏掉这类错误。 | 用真实点击来回切换，读取 Viewer 像素，检查静置后是否停止重算。[切图测试](../tests/native_photo_switch_ui.py) |
| 批量粘贴后多张照片引用同一原片或一处调整影响另一张 | 粘贴保留每个目标的 Reader 和原片，复制之后的效果，隔离可变节点组与生成的缓存。真实窗口里的增量 Undo 与后台操作也需要分别覆盖。 | 对两张目标粘贴，再改其中一张，保存重开并撤销整批。[批量调整测试](../tests/native_adjustments.py) |

**保留原生 Settings 子面板的原因。** 5.3 上用 `HIDE_HEADER` 隐去标题会让静止窗口持续重绘。若后续 Blender 提供新的面板接口，可用[空闲重绘探针](../tests/native_hidden_header_probe.py)测量改动前后 draw 次数，并在真实窗口核对拖拽、折叠和卸载。此项保留为后续版本的验证点。

## 2026-11-23 · 胶片校准、Mac OFX 与批量导出（0.20.0–0.20.3）

**Lite 与 OFX。** 0.20.0 加入独立的 Spektrafilm OFX 节点。Lite 可直接使用捆绑引擎；OFX 经独立 host 调用用户安装的完整官方插件。早期包遗漏了编译后的 Mac host。0.20.3 补齐 arm64 可执行文件，并在 ZIP 安装丢失执行位时恢复文件所有者的执行权限。排查顺序是检查 host 架构与权限、定位完整插件、核对插件版本，再渲染测试图检查曝光与 Alpha。[OFX 适配器](../blender_raw/spektrafilm_ofx_engine.py) · [Mac 运行测试](../tests/ofx_macos_runtime.py) · [实际像素测试](../tests/native_ofx_pipeline.py)

**从负片预设切换到反转片。** 含自定义 DIR 耦合系数的 Portra 预设切到 Velvia 后，旧系数可能使画面出现青色反转。0.20.1 调整了反转片的处理模式；0.20.2 又将导入的四组校准系数与来源胶片关联。切换时使用新胶片的校准，切回来源胶片时可恢复预设值。验证要包括一份带完整校准的预设，同时检查颗粒、扩散等独立设置是否保留。[校准用例](../tests/spektrafilm_calibration.py)

**同批照片导出了不同格式。** 三张照片原设置为 JPEG、PNG、PNG，在导出窗口选择 JPEG，旧 worker 仍可能按各自 Scene 的格式输出。0.20.3 在开始导出时冻结整批编码参数；选用 Follow Scene 时，各照片继续使用自己的视图设置。未显式传入输出设置的 `jobs.start_export` 调用仍可使用每张照片自己的格式。回归会读取三张实际生成的文件，并在导出途中修改界面设置，确认已经开始的批次不受影响。[队列实现](../blender_raw/jobs.py) · [导出回归](../tests/batch_export_format.py)

## 2026-11-24 · 裁切与首个公开版本（1.0.0–1.0.1）

**裁切与透视。** 1.0.0 把透视校正、旋转和裁切合并到同一套坐标变换，按实际预览或导出尺寸求采样。四条参考线校正的是整幅图像，随后由裁切框决定最终构图。几何测试用梯形线图和已知坐标的 EXR，检查实际输出尺寸、边缘、HDR 和 Alpha；窗口测试再检查拖动手柄、确认与取消。[几何实现](../blender_raw/crop_geometry.py) · [像素测试](../tests/crop_perspective.py) · [窗口测试](../tests/crop_perspective_ui.py)

**独立公开发行。** 1.0.1 确立扩展 ID `raw_photo_editor`。升级旧版时先在 Blender 偏好设置中停用 `blender_raw`，再启用新扩展。Windows 解码器改用 LLVM OpenMP；四份旧 DCP 从安装包移除，旧配方若指定了其中的文件，需重新选择相应的外部配置。发布包保留第三方许可证、原生代码来源和校验信息。[第三方依赖说明](../blender_raw/vendor/THIRD_PARTY.md) · [分发检查](../tools/verify_distribution.py)

本次公开发布对应提交 [007dbb3](https://github.com/shawnyhu/RAW-Photo-Editor/commit/007dbb3abf60574939d8642634faf4694b8faab3)。Apple Silicon 在 Blender 5.3.0 构建、安装并验证裁切交互；Windows x64 安装同一个 ZIP 并验证原生场景、批量导出和裁切像素。两个平台通过后发布安装包、源码包及各自的 SHA-256。GPU 后端的历史测量和与 DaVinci Resolve 的同图对比有各自的验收条件；公开发布流程没有执行 Resolve 对照。[公开 CI 记录](https://github.com/shawnyhu/RAW-Photo-Editor/actions/runs/45360386980) · [发行文件](https://github.com/shawnyhu/RAW-Photo-Editor/releases/tag/v1.0.1)

## 2026-11-25 · 在干净克隆中复核开发交接

以 `v1.0.1` 源码和全新的 Blender 用户配置，在 Windows x64、Blender 5.3.0 上按交接步骤构建并离线安装。安装测试读取了实际导出的 EXR 像素；批量测试确认三张照片都写成 JPEG，并保留原 Scene 设置；裁切测试覆盖 HDR、Alpha、保存重开和后台导出。另用出厂配置直接加载源码，验证了 Developer 在效果开关、重排后的源图绑定。这次 macOS 的依据是上一条公开 CI 记录。

从本次提交开始，新生成的源码包会携带根目录 `AGENTS.md`、开发交接和本日志。若要复现已经发布的版本，请使用对应标签；要取得最新交接记录，请读取 `main`。

## 2026-11-25 · 大图库的导出启动和缩略图排队（1.0.2-beta.1）

在 1.0.1 中，点击导出确认后，主线程为图库中的每张照片重复遍历所有 Developer，再保存项目快照。用 Windows x64、Blender 5.3.0 构造 200 张照片，每张各有 Reader、Developer 和 Spektrafilm Lite，只选一张导出：启动后台任务前的准备耗时 81.0 秒，其中 80.8 秒发生在保存快照之前，节点来源查询执行了 40,000 次。改为只沿所选照片启用的渲染链收集原片、外部配置和 LUT 后，同一测例耗时约 0.31 秒。测量止于创建后台进程之前，不包含 RAW 解码、Metal/Vulkan 或最终编码。导出仍同步保存项目快照，大量嵌入数据的写盘时间会受工程大小影响。[导出准备](../blender_raw/jobs.py) · [跨照片及嵌套绑定回归](../tests/native_source_binding.py)

导入后，活动照片的 Reader 已经能插队，但随后提交的 Developer 原先会排在旧缩略图之后。现在缩略图有独立队列，最多使用一个预览子进程；活动照片的 Reader 与 Developer 优先调度，总数限制为两个。缓存命中的缩略图也分多次 UI 更新处理。回归用 300 张待处理照片，故意延迟第一张缩略图，验证切图后的 Reader 与自定义白平衡 Developer 可以先完成。批量导入同时改为每批建立一次已有原片的路径索引。[调度回归](../tests/preview_scheduling.py) · [批量导入回归](../tests/import_batch_paths.py)

另在 Windows 的全新安装配置中，用同一份真实 Canon CR3 的 200 个独立文件名启动缩略图队列，打开第 125 项并指定 6700 K 白平衡。实际子进程用 4.62 秒完成该照片的 Reader 和 Developer，此时仍有 198 项缩略图排队。这个测试检查调度顺序，耗时只适用于该样张和测试机。

## 2026-11-26 · 裁切节点的单轴透视和大幅拖角（1.0.2-beta.2）

用户在 Mac 实测通过了上一版大图库修复。这次处理 Crop & Perspective 的三个问题。旧检测器会把照片上分散的边缘拼成两对参考线，直接用来校正整幅图。对照六张本地实拍，旧版在五张上应用了双轴透视，包括人物和落叶。新版要求同一方向两条间隔足够的长直线，检查边缘是否连续、其他线条是否支持同一透视，以及成片保留的原图面积和远近伸缩。六张实拍中，四张保留原构图，两张只有纵向通过。这个样本用于复现错误，无法给出普遍识别率。[检测实现](../blender_raw/crop_detect.py) · [合成图回归](../tests/crop_detection_confidence.py)

**Vertical** 取左右两条参考线，**Horizontal** 取上下两条，**Both** 按四边求透视。节点分别存储下次操作选取的方向和已经生效的方向，点击方向按钮、取消或检测失败均保留现有结果。旧节点缺少 `bounded_guides` 属性时沿用原有的采样边界，重新应用透视后才切换。用 beta.1 保存了一个大幅透视的真实 `.blend` 测试文件，beta.2 重开后四个角的反向采样坐标完全一致。[节点参数](../blender_raw/crop_node.py) · [像素、复制及保存回归](../tests/crop_perspective.py)

在 3:2 图像上用默认参考框固定三个角，只把左下角从图高 15% 向上拖。旧版到 74% 时，虽然四边形仍为凸形，整幅照片的投影已跨过消失线，编辑器丢弃了鼠标移动。新版只对可采样的区域拟合裁切；接近四点交叉时，拖动止于有效边界。独立配置安装后，通过 Blender 窗口事件把同一个角直接拖到 78% 并确认，另测了偏离手柄中心抓取、取消、横纵单轴和弱线条提示。导出的 EXR 检查了实际像素坐标、HDR 和 Alpha。[几何实现](../blender_raw/crop_geometry.py) · [几何回归](../tests/crop_axis_geometry.py) · [窗口测试](../tests/crop_perspective_ui.py)

公开测试包对应提交 [88bd66b](https://github.com/shawnyhu/RAW-Photo-Editor/commit/88bd66bea6359bfeac936df6ea4319598fbc787b)。Apple Silicon 在 Blender 5.3.0 完成离线安装、窗口拖角、透视几何、导出像素和批量导出回归，Windows x64 安装了同一个 ZIP 并通过几何与导出测试。分支另补了一张纵幅合成图，专门覆盖消失线穿过画面上部的情况，已在 Windows Blender 5.3.0 通过。[跨平台验证记录](https://github.com/shawnyhu/RAW-Photo-Editor/actions/runs/45735328785)

## 2026-11-27 · 调色效果卡片、Developer 测光和查看器背景（1.0.2-beta.3）

照片工作区的 **Add Scene Effect** 保留 Blender 资产目录和搜索，在目录前提供 **Adjust** 子菜单。九种卡片各自持有原生节点，在 Settings 里直接编辑；复制卡片或跨照片粘贴会得到独立参数和曲线。Gamma 在 Blender 5.3 中的节点类型是 `ShaderNodeGamma`，图像端口名为 `Color`，其余八种使用 `Image`。裁切后的输出尺寸沿同一条图像连接向前查找，因此在裁切后继续调色也能导出正确画幅。Spektrafilm Lite 和 OFX 单列一组，自定义节点组使用 **Custom Compositor**。[卡片映射](../blender_raw/adjust_effects.py) · [效果卡片回归](../tests/adjust_effect_cards.py) · [裁切后调色导出](../tests/crop_perspective.py)

Raw Developer 按 **Camera & Look**、**White Balance**、**Auto Exposure** 分组。**As Shot** 与 **Meter** 共用旧的 `auto_exposure` 值；取消 As Shot 会选择 Spot。保留所有已保存模式的枚举编号，包括两种早期胶片测光。原生效果卡片仍然使用同一个 Exposure 输入和动画路径。旧工程加载时只调整这个输入的界面可见性。测试覆盖普通节点和效果卡片、两个曝光关键帧、复制、保存重开及旧模式。[测光模式](../blender_raw/metering.py) · [Developer 回归](../tests/developer_meter_controls.py)

查看器的底色由 Photo Workspace 保存。Image Editor 按实际缩放和平移坐标填充照片外的区域，三档颜色分别为黑、深灰和浅灰。回归从真实窗口读取颜色，对比图内半透明像素、相邻图库、全局主题及导出 PNG，并检查工作区设置能随工程保存。[绘制逻辑](../blender_raw/viewer_background.py) · [窗口测试](../tests/viewer_background_ui.py)
