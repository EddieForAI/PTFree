<!-- SPDX-FileCopyrightText: 2026 EddieForAI
     SPDX-License-Identifier: Apache-2.0 -->

# PTFree 2.0.6 群晖版 · Freeseed 抢上传

新部署时，登录后进入「系统 → 全局配置 → 功能模块管理」启用所需模块，然后用侧栏「PT配置」旁的「＋」或该页的「＋ 添加站点」，选择 M-Team 并保存站点 API Access Token。左上角 PTFree 图标返回总运行概览。

从 MTFree 1.3.9 群晖版升级时，停止原项目并备份 `data` 与 `watch`。用新程序重建容器时，继续挂载原来的两个目录。


此包用于群晖 DSM 的 Container Manager 项目。项目根据 NAS 的 CPU 架构构建 Docker 镜像，适用于支持 Container Manager 的 **x86-64 或 ARM64** 机型。首次构建需要访问 Docker Hub、Debian 软件源和 Python 软件源。群晖版只运行 Freeseed 与统计采样。qB 自己的限速由 qB 管理。

## 部署到群晖

1. 在套件中心安装 **Container Manager**。用 File Station 将整个安装包解压到项目目录，例如 `/volume1/docker/PTFree-2.0.6-Synology-Freeseed`。项目目录中应直接看到 `docker-compose.yml`、`Dockerfile`、`ptfree/`、`data/` 和 `watch/`。
2. 在项目 YAML 中，把 `user: "1026:100"` 改成项目目录所有者的 **UID:GID**。用同一 DSM 账号通过 SSH 运行 `id` 可查看。这个账号须拥有 `data/` 和 `watch/`，并能在其中写入文件。若通过 File Station 解压后没有这两个目录，可在项目目录下用同一账号创建。
3. 在 **Container Manager → 项目 → 新增**，选择上面的项目目录，上传包内的 `docker-compose.yml`。选择创建并启动。若只创建了项目，在操作菜单中先「构建」再「启动」。
4. 在局域网浏览器访问 `http://群晖IP:8787`。首次管理密码在项目的 `data/初始管理密码.txt`，用步骤 2 的目录所有者账号读取。登录后可在「全局配置」修改密码。若 8787 已被占用，在 YAML 中把 `MTFREE_WEB_PORT` 和 `ports` 的两侧一起改成同一个空闲端口。

也可以经 SSH 进入该项目目录后运行：

```sh
sudo docker compose up -d --build
```

如果群晖无法从 Docker Hub 拉取基础镜像，可以在另一台与 NAS **同 CPU 架构**的 Docker 主机上构建，再通过 Container Manager 的「映像 → 导入」导入。导入后从项目 YAML 删除 `build:` 及其下属行，保留 `image: ptfree-freeseed:2.0.6-synology` 并创建项目。Python 依赖下载慢时，Compose 内有可选的 USTC PyPI 镜像设置。

```sh
docker compose build
docker save -o ptfree-freeseed-2.0.6-image.tar ptfree-freeseed:2.0.6-synology
```

## Freeseed 投递方式

| 方式 | PTFree 设置 | qB 设置 |
| --- | --- | --- |
| 目录投递 | 保存目录填容器内的 `/watch`。托管容量填 0。 | 监控同一个物理目录。qB 在另一容器时，把同一 NAS 目录也挂给 qB，两个容器内部的路径可以不同。 |
| Web API 直投 | 「全局配置」保存 qB WebUI 地址和认证。托管容量填正数，专用目录填写 **从 qB 端看到的绝对路径**。 | Windows 上的 qB 可用其局域网 IP，例如 `http://192.168.1.20:8091`，并让 NAS 能访问该端口。qB 5.2+ 可用 API 密钥。 |

如果 qB 继续留在 Windows，且要保持目录投递，可以把 NAS 项目的 `watch/` 分享给 qB 主机并在 qB 中配置可访问的共享路径，也可以把 NAS 已挂载的共享目录映射到容器 `/watch`。用 Web API 直投时，NAS 与 qB 之间只需要 WebUI 连通。

种子文件默认仅目录写入账号可读。qB 与 PTFree 都在群晖时，可让两个容器使用同一个 UID 访问投递目录。经 SMB 投递时，qB 监控目录所用的共享账号也需要能读取这些文件。

在「PT配置」保存 M-Team API 令牌和扫描选项。在 Freeseed 服务配置首项选择 M-Team，再设置通用筛选条件。先测试认证与目录读写，再预览一轮。确认后取消「仅预览」并启动 Freeseed。API 直投在容量满时可等待空间，也可按配置从最旧且确认归属的 Freeseed 任务开始回收。

## 首页统计

- **PPPoE 流量和下行峰值**。在「全局配置」设置 RouterOS API-SSL、可读账号和 PPPoE 接口。峰值为所选时段内最高的连续五分钟下行平均值。可选填 Ping 探测主机。
- **Freeseed 流量**。配置 qB Web API 后，从 qB 统计可确认归属的 API 直投任务和目录投递任务。旧版目录投递缺少哈希时，名称、体积和导入时间均能唯一核对的任务可恢复归属。两次 qB 采样间的累计增量按时段平均显示。
- **存储**。目录投递显示种子文件目录所在卷。qB Web API 另提供 qB 的默认下载卷空闲。启用直投时也显示实际预留配额。
- **历史曲线**。按日、周、月、年或累计查看。PPPoE 与 Freeseed 曲线共用 Mbps 纵轴，图下显示流量总量和采样覆盖率。停止扫描期间也会继续采样。

## 持久化和迁移

`data/` 保留配置、SQLite 历史、管理密码和加密密钥。升级或备份时先停止项目，再备份整个目录。重建容器时沿用原数据挂载。首次配置使用空目录即可。

从 Windows 或 Linux 迁移时，先暂停旧实例的 Freeseed，再停止旧实例并备份其数据目录。将数据目录完整复制到本项目的 `data/`，包括 `secret.key`。以新架构启动后，在网页把种子保存目录改成 `/watch` 或实际容器路径，并测试 qB 访问。若 qB 的 WebUI 地址发生变化，重新保存对应凭据。既有 Freeseed 直投归属记录按原 qB URL 和任务路径保留，改址后可先在 qB 核对当前存量。此版统计独立从部署后开始采集。

## 开源许可

© 2026 EddieForAI。项目代码采用 [Apache License 2.0](LICENSE)。开发署名见 [NOTICE](NOTICE)，第三方条款见 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)。
