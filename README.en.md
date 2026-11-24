<!-- SPDX-FileCopyrightText: 2026 EddieForAI
     SPDX-License-Identifier: Apache-2.0 -->

# PTFree

[简体中文](README.md) · English

PTFree is a self-hosted private tracker management tool with a Chinese web interface. It currently supports M-Team. Use it to find freeleech torrents and monitor tasks, connections, and traffic from a single dashboard.

- **Freeseed** finds freeleech torrents within your selection. Preview the results before sending torrents to a watch directory or to qBittorrent.
- **Traffic Assistant** manages hosted tasks, disk quotas, and bandwidth schedules on Windows and Linux.
- **Tracker Settings** holds your M-Team API token, search categories, and site-wide freeleech dates. Each module has its own tracker selection.
- **Overview** shows connection status, module activity, and available traffic statistics.

## Install

Choose an installation ZIP for your platform on the repository's **Releases** page.

| Platform | Release asset | Quick start |
| --- | --- | --- |
| Windows x64 | `PTFree-<version>-Windows-x64.zip` | Extract the ZIP, run `1-Install_Service.bat`, then run `3-Start_Service.bat`. See the [Windows guide in Chinese](README-中文.md#windows-安装). |
| Linux x86_64 | `PTFree-<version>-Linux-x86_64-Installer.zip` | Extract the ZIP, run `bash install-linux.sh`, then run `./run-linux.sh`. See the [Linux guide in Chinese](README-中文.md#linux-安装ubuntu-2404-x86_64). |
| Synology | `PTFree-<version>-Synology-Freeseed.zip` | Extract the ZIP and create a Container Manager project from `docker-compose.yml`. See the [Synology guide in Chinese](synology/README-群晖.md). This edition includes Freeseed. |

On the first run, read the admin password from `初始管理密码.txt` in the data directory. Sign in, enable the desired modules under System → Global Settings, then add M-Team under Tracker Settings and enter your API token. To upgrade, stop the existing service and reuse its data directory.

## Develop and contribute

The desktop implementation is in `ptfree/`. The Synology edition is in `synology/`. Use Python 3.11 or later in a virtual environment and run these commands from the repository root.

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests
python -m ptfree run
```

Issues and pull requests are welcome. Remove API tokens, private tracker passkeys, and admin passwords from examples and logs. See [PUBLISHING.md](PUBLISHING.md) for packaging instructions.

## Copyright and license

© 2026 EddieForAI. PTFree was initiated and is maintained by EddieForAI, with development assistance from ChatGPT. The project code is licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for attribution and [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) for independent components. M-Team and qBittorrent are maintained by their respective owners.
