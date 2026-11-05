# AOA 安全标定页面

录制机需要安装此应用后才能进行自动探针标定。应用不申请权限、没有按钮，
仅消费触摸事件并显示落点。Worker 固定打开此 Activity，失败时不发送探针，
也不再降级到 Settings 页面做单点点击。

推荐使用现有 JDK 和 Android SDK 离线构建：

```bash
python tools/scripts/utilities/build_aoa_calibration.py --sdk-root /path/to/android-sdk
```

默认使用 Android 35 与 build-tools 35.0.0，可通过 `--platform`、`--build-tools`
指定已安装版本。输出为 `dist/aoa-calibration-debug.apk`，会验证签名，不覆盖
已有输出；重复构建可用 `--output` 指定新文件。调试签名密钥只在临时目录生成，
构建后销毁，因此不同次构建不保证能覆盖安装，请使用同一 APK 或自行稳定签名。
构建脚本不会安装应用或连接设备。

也可以使用 JDK 17、Gradle 8.9 和 Android SDK 35 构建（首次需要下载 Gradle 插件）：

```bash
gradle -p worker_agent/calibration_app assembleDebug
```

生成 APK 位于 `worker_agent/calibration_app/build/outputs/apk/debug/`。
由操作者在 ADB 可用的录制机上安装 APK。Worker 启动组件为
`com.gms.remote.aoacalibration/.CalibrationActivity`。此应用不安装到无 ADB 的目标机。

五个拟合点之外另测两个点；独立验证失败或 USB 清理失败时不发布已验证映射。
构建产物、签名密钥和本机 SDK 路径不应提交到仓库。
