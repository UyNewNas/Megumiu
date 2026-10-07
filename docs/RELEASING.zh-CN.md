# 构建与发布

当前发行包是第一阶段开发预览：提供命令行、模拟数据和相机采集诊断，不代表已经实现完整人体动捕或 SteamVR 驱动。没有安装包和自动更新机制。

## 持续集成

提交分支或提交 Pull Request 后，`CI` 在 Ubuntu、Windows 上分别使用 Python 3.11 和 3.13：

1. 安装 `.[camera]`，检查 Python 源码能否编译，运行 `unittest`。
2. 运行命令行帮助和两秒、60 FPS 的模拟采集。
3. 构建 wheel 与源码分发包。
4. 在 Windows / Python 3.13 上构建便携版，再运行真正的 `megumiu.exe --help` 和模拟采集。

成功后，Actions 页面提供 `python-distributions` 和 `windows-portable` 两个构建产物，保留 14 天。这些自动检查不需要相机，不验证真实相机同步、姿态准确度、SteamVR 接入或物理端到端延迟。

## 本地构建 Windows 便携版

需要 Windows x64、Python 3.11 或更新版本，以及可访问 Python 包仓库的网络。推荐使用独立虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\scripts\package_windows.ps1 -Python .\.venv\Scripts\python.exe
```

脚本自动安装当前仓库的 `camera` 依赖和固定版本 `PyInstaller==6.22.3`，在 `build/portable-<随机编号>/` 构建并运行 smoke test，输出 `dist/windows/megumiu-<版本>-windows-x64.zip`。可以通过 `-OutputDirectory` 指定输出目录。脚本保留构建目录以便排错，重复执行不会复用旧可执行文件。

将 ZIP **完整解压**后，在终端中运行目录内的 `megumiu.exe`。`_internal` 目录包含 Python 运行时、OpenCV 和 NumPy 等依赖，必须与可执行文件保存在一起；不需要用户另外安装 Python。便携版尚未进行 Windows 代码签名。

## 创建待审核发行版

版本号以 `pyproject.toml` 的 `[project].version` 为准，标签必须严格等于 `v` 加该版本。例如 `version = "0.1.0"` 对应 `v0.1.0`；`0.1.0a1` 对应 `v0.1.0a1`。

维护者准备好版本并完成代码审核后，自行创建并推送对应版本标签。工作流不会创建或推送标签。也可以在 Actions → `Draft release` → `Run workflow` 中输入一个已经存在的版本标签，重新构建该标签的提交。

发布流程会验证标签存在、检出提交与标签相同、标签与包版本一致，然后重新运行上述完整 CI 矩阵。全部通过后才创建 **draft + prerelease**，附带：

- Windows x64 便携 ZIP。
- Python wheel 和 `.tar.gz` 源码分发包。
- `SHA256SUMS.txt`，包含上述三个文件的 SHA-256。

只有创建草稿的最后一个 job 拥有 `contents: write`；测试和构建仅有读取权限。该 job 不执行源码或构建程序。重复运行可以更新同一标签的草稿预发布资产；若发行版已经公开，或者已经取消预发布标志，流程会失败，不会覆盖它。

维护者应下载并核验哈希，在真实 Windows 环境试运行，补全已知限制和版本说明后，再在 GitHub Releases 页面手动发布。工作流不会自动公开发行版，也不会上传 PyPI。

## 工具版本依据

截至 2026-10-07，工作流采用官方发布的 [checkout 7.0.1](https://github.com/actions/checkout/releases/tag/v7.0.1)、[setup-python 7.0.0](https://github.com/actions/setup-python/releases/tag/v7.0.0)、[upload-artifact 7.0.1](https://github.com/actions/upload-artifact/releases/tag/v7.0.1) 和 [download-artifact 8.0.1](https://github.com/actions/download-artifact/releases/tag/v8.0.1)。Windows 打包固定为 [PyInstaller 6.22.3](https://pyinstaller.org/en/v6.22.3/)；升级时应重新验证 Python 3.13 和 OpenCV 的打包结果。
