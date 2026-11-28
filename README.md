# RAW Photo Editor 1.1.1 · Windows x64 验收

将 ZIP 解压到可写目录，例如 `C:\Temp\rpe111`。在含有 `RUN-TESTS.cmd` 的目录运行：

```powershell
.\RUN-TESTS.cmd
```

需要 Windows x64 和可用的 Vulkan 显卡驱动。启动器会使用本机的 Blender 5.3，在临时配置中离线安装扩展。自定义安装位置可通过环境变量 `RPE_BLENDER_EXE` 指向 `blender.exe`。若未找到 Blender，启动器会下载官方 Blender 5.3.0 便携版、核对 SHA-256，在临时目录运行。运行所需的扩展、测试和测试图像生成代码均在包内。

验收覆盖 64 款胶片的切换、效果开关与模式保留、Film Defaults 完整重置、保存重开，以及 Verita 200D 搭配 Production 颗粒和 Black Pro-Mist 的实际 Vulkan 像素对照。另有 15 条黑白路径、24 组黑白颗粒渲染、胶片菜单、校准、个人预设与 EXR 导出测试。

每次运行都会写入独立的 `results` 子目录。命令返回 0 表示通过，返回 1 表示失败。将最后打印的 `results\raw_photo_editor-1.1.1-windows-*.zip` 交回发布端。包内含 `report.json`、Blender 版本和各步骤日志。启动阶段出错时，报告会记录失败原因。运行时无需管理员权限。

| 验收对象 | 值 |
| --- | --- |
| 安装包 | `raw_photo_editor-1.1.1.zip` |
| 安装包 SHA-256 | `3690cca8ecee03bae2b8f7a75e4809309d052947843782fbf12de7996b49f038` |
| 版本源码及测试 | [4effe368](https://github.com/shawnyhu/RAW-Photo-Editor/tree/4effe368557965ba374d4be7b0566c16b321ec99) |
