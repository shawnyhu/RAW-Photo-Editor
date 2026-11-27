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

测试包对应提交 [e9ee18b](https://github.com/shawnyhu/RAW-Photo-Editor/commit/e9ee18b2f2f501c63408b27200ce243505b9ce95)。Windows 本地用 beta.2 创建工程，再用 beta.3 重开，核对四种已存测光模式、原生曝光输入上的两帧动画以及两个既有自定义效果。公开 CI 在 Apple Silicon 的 Blender 5.3.0 窗口检查了九张卡片的实际控件、曲线编辑器和查看器三档背景；Windows x64 安装同一份 ZIP 完成后台回归。安装包 SHA-256 为 `47f288cd7b94da275f651117dc8cf43df20570eb2f75f1a6af2a9bb3636d2460`。[验证记录](https://github.com/shawnyhu/RAW-Photo-Editor/actions/runs/45930095218)

## 2026-11-27 · Spektrafilm Lite 用户预设和查看器网格（1.0.2-beta.4）

Lite 现在从当前节点保存预设。之前的外部文件导入操作也会写入上游 Developer 的部分设置，新入口只读写胶片参数。保存的内容涵盖胶片、相纸、工艺、颗粒、扩散、扫描，以及既有节点从旧预设继承的校准系数。套用前先验证整份设置，更新后合并请求一次预览。节点的播放状态、折叠状态、原片及上游设置由目标工程管理。[参数与校准](../blender_raw/film_user_settings.py) · [参数边界回归](../tests/spektrafilm_user_settings.py)

预设在当前系统用户的 Blender 配置目录下，每个预设一个 JSON 文件。路径独立于 Blender 版本和扩展安装目录，重命名保留文件 ID，写入时用临时文件原子替换。备份和换机路径见[用户指南](workflow.md)。Windows 5.3.0 离线安装同一份 ZIP 后，在两个独立进程的新工程间保存、重命名、应用和删除预设，并验证目标 Live Preview 开关的两种状态。窗口测试在第二张卡片选中时，点开第一张的 Presets 并套用，通过两张不同的颗粒量校验实际操作对象。[跨进程回归](../tests/spektrafilm_user_preset_flow.py) · [卡片点击回归](../tests/spektrafilm_user_presets_ui.py) · [持久化与写入冲突](../tests/film_user_library.py)

Grid 只在创建照片工作区查看器时设置默认值。Windows 5.3.0 窗口验证了新工作区关闭网格，用户手动打开后重新进入工作区、保存并重开工程均能保留。[查看器回归](../tests/viewer_background_ui.py)

公开测试包对应提交 [7f3555e](https://github.com/shawnyhu/RAW-Photo-Editor/commit/7f3555e3ebd63dfb1aacb1acd267daad5018fc2d)。Apple Silicon 和 Windows x64 用 Blender 5.3.0 安装同一个 ZIP，通过了两次独立启动之间的预设保存与调用。另在 Windows 将同一个系统账户指向两个全新的插件安装配置，使用真实的用户预设路径，第二套配置仍可读取第一套保存的胶片参数。之后的 Blender 大版本需在对应版本发布时再次验证扩展兼容性。安装包 SHA-256 为 `d7704cb93d17570b729f85c927fdf5bf5effa76b28d6f0b6252d3ea654eec55f`。[跨平台验证记录](https://github.com/shawnyhu/RAW-Photo-Editor/actions/runs/45947138524)

## 2026-11-27 · Lite 完整内置预设与材料扩充（1.0.2-beta.5）

四份用户保存的 FUJI Pro 400H、FUJI Provia100f、KODAK Ektar 100、KODAK Portra 800 JSON 原样内置，按字节与原件核对。应用时恢复完整配方，保留目标 Live Preview；同名个人副本继续独立保存、重命名和删除。[资源](../blender_raw/assets/film_presets/) · [预设库](../blender_raw/film_user_library.py) · [跨进程验收](../tests/spektrafilm_user_preset_flow.py)

材料选择器由 20 款胶片、8 款印片扩至 56/12。新增 32 款胶片与四款 Fujifilm Eterna-CP 印片来自固定版本 Negs；Kodak Vision2 50D、100T、500T 和 UltraMax 800 来自 The Dyeworks 2.2.3。仅追加原库缺少且来源、许可和校准完整的数据，保留原索引。新增材料的光谱、特性曲线与印片滤镜编译到两端原生库；选胶片时同步其 DIR、光晕与颗粒参数。新增反转片选中后采用 Negative only，Print only 的独立选择保持。四款 Dyeworks 与四款新印片的 16 对 TH-KG3 滤镜由开源参考程序求解。UltraMax 800 沿用同系列 400 的光谱模型并使用自身特性曲线，来源还含其他继承参数；模型限制及逐款归属见[材料变更记录](../blender_raw/vendor/spektrafilm/CHANGELOG.txt)和归档内 JSON。运行无需安装 OFX。

**最后一次构建修正。** 四款 Dyeworks 初次构建使用了通用负片 DIR 分组；最终数据改用各自来源的同层及 R/G/B 层间分组。`additional-stock-data.tar.gz` 的 Git blob 为 `5961ab97724b62834330d8b487c2e4495323d3fb`，SHA-256 为 `4815ffee01ae69502cb61fd1b275d86f9c7c0f11335f15d70bad8fa41006613e`。Windows 在现成 LLVM-MinGW/Vulkan 工具链中重建，结果与此前修正产物逐字节相等；原生 copy、core 和多 tile/full-frame 像素对照通过。DLL SHA-256 为 `8902e69a95170eebe1a6c2147f297758bc2ad2ff3222bafea02ed73305fc42e9`。

Apple Silicon 取自[最终构建](https://github.com/shawnyhu/RAW-Photo-Editor/actions/runs/45952613175)，构建提交 `b9bf4d6`。归档摘要与 CI 一致，arm64、严格签名及 56/12 型号检查通过；从 Mach-O 符号地址读取四款真实 DIR 的全部分组，与修正数据一致。dylib SHA-256 为 `19a700d2c9e3a47fa07fd3de22e09083fc15e30a8496a69aa08ab16ae87b7d30`。两端构建输入已核对一致，着色器和原有共享浮点表保持原内容。

**最终候选安装包的 Windows 验收。** Blender 5.3.0、NVIDIA GeForce RTX 4090，在新用户配置中离线安装 ZIP，通过以下项目：

- 使用既有 beta.4 的同机基准逐像素比较原有 20×8 配对，160 组完全一致，原索引和选择顺序一致。基准 SHA-256 为 `907f851318095ba838039faa2ebe63c12302b7093db66c05d309ff4559721f97`，未重新录制或覆盖。全部 512 组新增配对输出有限且灰阶非平坦。[配对测试](../tests/spektrafilm_stock_pixels.py)
- 36 款新增胶片校准、四款 Dyeworks 全部 DIR 分组、负片/反转片/印片及生产颗粒实际 GPU 输出通过；36 款逐一编辑并保存重开，完整配方和印片选择保持。[新增校准](../tests/spektrafilm_added_calibration.py)
- 既有 Portra→Velvia 实际像素、完整校准继承、复制与保存重开通过。[原有校准](../tests/spektrafilm_calibration.py)
- 四份内置预设的完整参数恢复、Live Preview 开/关保持、已有同名个人副本管理、跨进程个人预设通过；真实窗口点击确认非活动卡片能正确应用预设。原生离线安装、依赖、Scene 与 EXR 导出像素通过。

源码打包检查现覆盖允许目录中的未忽略、未跟踪文件，显式要求附带新增数据、两个生成脚本、grain 表、patch、配对滤镜、四份预设以及五份来源许可/归属文件。安装包逐文件与工作副本及源码包核对。[分发检查](../tools/verify_distribution.py)

安装包 SHA-256：`178331fa74af838e1eda53d1c8f8896cd957a8ac97afe7f7295472aaee5e309d`。本地日志在 `dist/beta5-final-validation/`，原生构建证据在 `dist/stock-rebuild/`。测试包对应提交 `bf0247d`，已发布为 [v1.0.2-beta.5 预发布版](https://github.com/shawnyhu/RAW-Photo-Editor/releases/tag/v1.0.2-beta.5)。Mac CI 本次仅提供构建证据；随后依据[Mac Codex 交接 prompt](spektrafilm-beta5-mac-handoff.md)在用户实机完成的结果见下条记录。

## 2026-11-27 · Apple M1 实际 Metal 验收回传（1.0.2-beta.5）

用户回传 Mac Codex 的实机验收摘要：macOS 26.7.3、Apple M1 七核 GPU、Metal 4、Blender 5.3.0。使用用户启动的桌面会话，在独立 Blender 配置中离线安装最终包，全程无需 OFX，报告的 17 项验收阶段全部 PASS。安装包及 arm64 dylib 摘要与本地和 GitHub 已发布文件一致，dylib 严格签名检查通过。

- 在该 M1 上用 beta.4 的 `record` 生成基准，再用最终 beta.5 `compare`：原有 160/160 配对逐像素完全一致，胶片与印片索引一致；512/512 新增配对全部实际渲染，输出有限且灰阶非平坦。Mac 基准 SHA-256 为 `7c46106d07774f78d4dc4817e288d6227692c866cc1682900bde08a921ca3759`，按报告以只读方式保留。
- 四款 Dyeworks 全部 DIR 分组、Portra→Velvia、负片/反转片/印片/颗粒渲染通过。36 款新增胶片保存重开后保留印片选择与编辑参数。
- 四份完整预设恢复全部参数并保留目标 Live Preview；同名个人副本可独立重命名、删除。真实窗口菜单包含四份预设；第二张卡片活动时，对第一张卡片的预设点击只修改第一张。原生 EXR 导出像素通过。

首次受限进程无法访问 Metal 的失败日志已由 Mac 端保留；最终通过结果来自用户启动的桌面会话。两张窗口截图内容相同，不能充当前后视觉变化对照；点击行为的证据为真实窗口事件及节点数值断言。

本条依据用户回传报告摘要记录。当前 Windows 会话未能读取用户 ChatGPT Library 中的原始验收归档，尚未独立复核该归档的日志、基准与截图；上述 Mac 结果不计作 Windows 端重跑或 CI 结果。当前验收覆盖 Windows RTX 4090/Vulkan 与该 Apple M1/Metal 两套环境。保留已验收安装包和原生库字节及原标签，在原预发布页补充 Mac 报告，不重新构建安装包。
