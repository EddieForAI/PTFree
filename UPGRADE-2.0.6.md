# Windows 升级到 PTFree 2.0.6

1. 在旧目录运行 `0-Stop_Service.bat`。
2. 备份 `%LOCALAPPDATA%\MTFree`，保留数据目录，不删除数据库或配置。
3. 将 Windows x64 完整包解压到新的独立目录。内含 runtime，无需复制旧运行时。
4. 运行 `1-Install_Service.bat`、`2-Enable_AutoStartSystemService.bat`（可选）、`3-Start_Service.bat`。
5. 登录后刷新网页（Ctrl+F5），确认版本 v2.0.6。等两轮 qB 采样，检查「总运行概览」里的 PTFree 上传。

升级后保留配置、任务和历史统计。旧版目录投递记录如果缺少哈希，PTFree 会按名称、体积与加入时间核对，唯一匹配的任务继续计量下载和做种上传。相同 qB 任务的累计计数可以跨短暂断连接续，较长间隔按整段平均显示。图表标明新统计开始的时间，早期漏记的字节没有可用于重建时间序列的样本。
