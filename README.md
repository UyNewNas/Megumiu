# Megumiu

三相机纯视觉动捕项目，目标是在保留 VR 头显和手柄的情况下，无需身体绑带地追踪腰和双脚，并把位姿交给 SteamVR。

当前为 **0.1.0a1 第一阶段开发版**：采集与测量工具已实现，真实三相机验收待完成。尚无人体姿态推理、相机空间标定或 SteamVR 输出。P95＜100ms 是后续完整追踪链路的验收目标，10–20ms 是探索目标，均不是当前性能声明。

- [项目计划书](docs/PROJECT_PLAN.zh-CN.md)
- [第一阶段硬件验收指南](docs/HARDWARE_VALIDATION.zh-CN.md)
- [第一阶段开发与验证记录](docs/PHASE1_STATUS.zh-CN.md)
- [构建与发版](docs/RELEASING.zh-CN.md)

## 目前能做什么

- 每台相机独立进程采集，记录实际图像尺寸、请求参数与驱动读回值。
- 使用同一主机单调时钟记录读取起止；按到达时间形成候选帧组，限制排队并统计丢弃。
- 输出逐帧元数据、配对记录和 P50/P95/P99 报告；启动失败、断流或卡住时返回失败状态。
- 无相机模拟、记录回放、共同闪光事件的粗略到达差分析。
- 默认只保存元数据；显式开启录像时保存本地 MJPEG AVI。

**主机到达差不是曝光同步误差，读取耗时不是图像年龄。** 报告固定保留 `exposure_sync_verified: false` 和 `end_to_end_latency_ms: null`，避免将未测指标显示为零。`observed_fps` 是读取交付速率，没有传感器帧号时不能证明每一帧都是新的曝光。

## 安装与无相机试跑

开发环境需要 Python 3.11 或以上。在仓库目录打开终端：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[camera]"
.\.venv\Scripts\python.exe -m megumiu doctor
.\.venv\Scripts\python.exe -m megumiu simulate --duration 10 --fps 60 --output captures/demo
.\.venv\Scripts\python.exe -m megumiu replay --input captures/demo --output captures/demo-replay
```

Linux 用 `.venv/bin/python` 替换上述 Python 路径。仅运行模拟、回放和统计时，安装 `pip install -e .` 即可，无需相机依赖。模拟按虚拟时间生成数据，运行很快；输出不代表真实硬件吞吐率。

Windows CI 会生成便携 ZIP。解压全部文件后，在解压目录运行 `./megumiu.exe --help`，无需另装 Python。`_internal`、`configs` 与 `docs` 应与程序一起保留。当前未发布正式 Release，可从成功的 GitHub Actions 运行中下载 `windows-portable` 产物。

## 三相机采集

复制并修改 [configs/cameras.example.json](configs/cameras.example.json)，填写设备编号、支持的帧率和分辨率。默认三个设备编号是 0、1、2，不保证与你的插口顺序对应。先只保留一个相机条目验证，再增加到三台。Windows 可尝试 `dshow` 或 `msmf`，Linux 使用 `v4l2` 或 `auto`。

```powershell
.\.venv\Scripts\python.exe -m megumiu capture --config configs/cameras.example.json --duration 30 --output captures/three-camera-baseline
```

便携版对应：

```powershell
.\megumiu.exe capture --config configs/cameras.example.json --duration 30 --output captures/three-camera-baseline
```

相机预热默认 1 秒；启动或断流超时默认 5 秒，可用 `--warmup` 与 `--timeout` 调整。三路准备完成后的统一启动信号只控制软件读取，不能触发传感器同步曝光。某路卡住会终止该进程并将运行标记为失败。按 Ctrl+C 可中止并保留已有报告。

输出目录必须是新目录，工具不会覆盖已有会话。连续采集支持最长 3600 秒；硬件验收用 `--duration 600` 做 10 分钟测试。原始元数据暂存在内存中以汇总分位数，长时高帧率采集需要相应内存。

`--record` 额外保存每路 AVI，编码和磁盘写入会影响时序，应与不录像基线分开测试。AVI 按请求帧率封装，帧的真实交付时间以 `frames.jsonl` 为准；强制结束时 AVI 可能不完整。当前工具无实时预览界面。

## 配置注意事项

每台相机可设置 `brightness_roi: [x, y, width, height]`，单位为实际图像像素，取共同 LED 信号所在区域；省略时统计全图灰度均值。ROI 越界会报告错误。

可选 `exposure` 和 `auto_exposure` 原样传给 OpenCV 后端，单位与数值含义取决于相机和驱动，不存在跨设备统一的“关闭自动曝光”数值。默认不改曝光。`buffer_size: 1` 只是请求，驱动可能不支持。每项设置的返回值与读回值在报告中保留，不能据此保证驱动内部无缓冲。

参数说明参见 [OpenCV VideoCapture](https://docs.opencv.org/4.x/d8/dfe/classcv_1_1VideoCapture.html) 与 [视频输入属性](https://docs.opencv.org/4.x/d4/d15/group__videoio__flags__base.html)。

## 输出与光学对照

每个会话包含：

| 文件 | 用途 |
| --- | --- |
| `session.json` | 版本、配置、模式、时钟口径与实际采集时长 |
| `frames.jsonl` | 每帧编号、读取起止、尺寸、ROI 亮度；默认不含图像 |
| `pairs.jsonl` | 不复用帧的候选组和主机到达差 |
| `report.json` | 分位数、配对数量、丢弃、未配对、错误和驱动参数 |
| `<camera_id>.avi` | 仅在 `--record` 时存在 |

`dropped_frames` 指配对器主动丢弃；`ipc_dropped_frames` 指进程传递队列已满。传感器丢帧、驱动重复帧和上游丢失没有硬件计数器时无法判定。`unpaired_frames` 包括被丢弃以及仍等待匹配的帧。

让三台相机拍摄同一个物理 LED，并设置覆盖信号的 ROI，固定曝光和增益后，可生成粗略对照：

```powershell
.\.venv\Scripts\python.exe -m megumiu flash-report --input captures/three-camera-baseline --threshold 128 --match-window-ms 100
```

该命令寻找亮度跨阈值的上升沿，默认以第一台相机为参考，在匹配窗口内逐一对应，且不复用事件。脉冲间隔应大于两倍匹配窗口，并人工核对漏检或错配。它测到的仍是“同一光学变化首次被交付到主机的时间差”，受帧间隔、曝光和传输影响；无法单独解出曝光差，也不能测绝对采集延迟。没有可用事件时结果为 null，不代表零延迟。物理验收步骤见硬件指南。

## 开发与交付

```powershell
python -m unittest discover -s tests -v
python -m pip install build
python -m build
```

CI 在 Windows / Ubuntu、Python 3.11 / 3.13 上运行测试、模拟 smoke 和包构建，另验证 Windows 可执行文件。版本标签流程创建草稿预发布并附 SHA-256；公开发布仍需检查产物和硬件状态。仓库不自动下载模型、不联网传输相机画面。`captures/`、构建结果和本地环境已默认忽略。
