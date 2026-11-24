<!-- SPDX-FileCopyrightText: 2026 EddieForAI
     SPDX-License-Identifier: Apache-2.0 -->

# PTFree · PT小管家

简体中文 · [English](README.en.md)

PTFree 是一个自托管的 PT 管理工具，提供中文网页界面。当前支持 M-Team。适合希望自动发现免费种子，同时查看任务、连接和流量统计的用户。

- **Freeseed 抢上传**按设定范围查找免费种子，可先预览筛选结果，再投递到监控目录或交给 qBittorrent。
- **流量助手**在 Windows 和 Linux 版提供托管任务、容量管理和带宽调度。
- **PT配置**集中保存 M-Team 令牌、搜索分区和全站免费时间。两个模块可分别选择适用站点。
- **总运行概览**显示连接、任务状态与可用的流量统计。

## 安装

在仓库的 **Releases** 页按平台选择安装包。

| 平台 | 安装包 | 安装说明 |
| --- | --- | --- |
| Windows x64 | `PTFree-<版本>-Windows-x64.zip` | [Windows 安装](README-中文.md#windows-安装) |
| Linux x86_64 | `PTFree-<版本>-Linux-x86_64-Installer.zip` | [Linux 安装](README-中文.md#linux-安装ubuntu-2404-x86_64) |
| 群晖 | `PTFree-<版本>-Synology-Freeseed.zip` | [Container Manager 安装](synology/README-群晖.md) · Freeseed 专版 |

首次安装后，用数据目录里的 `初始管理密码.txt` 登录。到「系统 → 全局配置」启用所需模块，再在「PT配置」添加 M-Team 并保存自己的 API 令牌。已有数据的用户可以停止旧服务后，沿用原数据目录升级。

## 开发与贡献

桌面版源码位于 `ptfree/`，群晖版位于 `synology/`。使用 Python 3.11 或更新版本，在虚拟环境中安装依赖。下列命令从仓库根目录执行。

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests
python -m ptfree run
```

欢迎在 Issues 提交问题，或通过 Pull Request 提交改进。提交复现信息时，请移除 API 令牌、下载链接中的密钥和管理密码。打包与发布步骤见 [PUBLISHING.md](PUBLISHING.md)。

## 版权与授权

© 2026 EddieForAI。项目由 EddieForAI 发起和维护，开发过程中使用 ChatGPT 辅助实现。项目代码采用 [Apache License 2.0](LICENSE)。署名见 [NOTICE](NOTICE)，独立组件的条款见 [第三方说明](THIRD-PARTY-NOTICES.md)。M-Team 和 qBittorrent 为各自维护者的项目。
