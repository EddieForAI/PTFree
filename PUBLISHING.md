<!-- SPDX-FileCopyrightText: 2026 EddieForAI
     SPDX-License-Identifier: Apache-2.0 -->

# PTFree 发布说明

## 源码进仓库

根目录 `README.md` 是中文版首页，`README.en.md` 是英文版。`LICENSE`、`NOTICE` 和 `THIRD-PARTY-NOTICES.md` 应随代码提交。`synology/` 是群晖构建上下文，`tools/` 保留 Windows、Linux 和群晖的打包入口。

- 已有远程仓库时，在 GitHub Desktop 用 **File → Clone repository** 克隆它。解压源码包，把顶层 `PTFree` 目录里的内容放到克隆目录。核对 Changes，提交后用 **Push origin**。保留克隆目录中的 `.git`，沿用现有提交历史。
- 首次建仓时，在 GitHub Desktop 用 **File → New repository** 创建仓库，再把源码内容放入它的本地目录。提交后用 **Publish repository**。想先在私有仓库核对时，保留 **Keep this code private**。

部署数据、账户配置、运行时、依赖 wheel 和安装包都放在发布输入目录或本机数据目录。`.gitignore` 已覆盖这些路径。仓库已经存在同名文件时，在 Changes 中核对实际替换内容。

## 安装包进 Release

GitHub 会为标签自动生成源码 ZIP 和 tar.gz。`PTFree-2.0.6-Source.zip` 用于初次导入仓库。Release 的 Assets 里上传 Windows x64、Linux x86_64 Installer 和群晖 Freeseed 的三个安装 ZIP。

1. 确认 `2.0.6` 的提交已推送。在 GitHub 仓库的 **Releases → Draft a new release** 选择该提交所在分支，创建 `v2.0.6` 标签。
2. 标题填 `PTFree 2.0.6`，写发布说明并添加三个安装包。检查后保存草稿或发布。

2.0.6 恢复可核对的旧版 Freeseed 目录投递归属，保留长间隔的 qB 流量增量，并在统计页标明旧数据可能漏计的时段。升级沿用现有数据目录。

2.0.6 的 Windows 安装包复用已校验的 2.0.5 Windows 嵌入运行时，Linux 安装版复用 2.0.5 的依赖 wheel。按以下命令生成安装包及源码包。

```sh
python tools/package_206.py --windows-baseline /path/to/PTFree-2.0.5-Windows-x64.zip --linux-baseline /path/to/PTFree-2.0.5-Linux-x86_64-Installer.zip --output release-assets
```

## 从源码重建安装包

使用 Linux x86_64 的独立构建环境。先安装 `requirements.txt` 和 `PyInstaller==6.22.3`。准备与目标平台匹配的 wheel、官方 CPython 3.14.10 Windows x64 嵌入包，以及发布运行时适用的 Python 和 OpenSSL 许可文件。

```sh
python -m pip install -r requirements.txt PyInstaller==6.22.3
python tools/build-native.py
python tools/package_release.py \
  --windows-python /path/to/python-3.14.10-embed-amd64.zip \
  --windows-wheels /path/to/windows-wheels \
  --linux-wheels /path/to/linux-wheels \
  --linux-binary dist/ptfree \
  --linux-python-license /path/to/CPython-LICENSE.txt \
  --openssl-license /path/to/OpenSSL-LICENSE.txt \
  --include-synology \
  --output release-assets
```

`--windows-wheels` 需要对应 Windows x64 的 cryptography、cffi 和 pycparser 依赖。Windows 包也可以通过 `--windows-baseline` 复用已在打包脚本登记校验值的旧安装包运行时。`--linux-wheels` 用于源码安装的离线依赖。群晖包由 `synology/` 生成，可在目标 DSM 通过 Container Manager 构建。项目许可文本更新时，同步 `ptfree/static/legal/` 与群晖对应目录中的网页副本。
