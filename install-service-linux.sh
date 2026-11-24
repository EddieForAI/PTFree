#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
if [[ "${1:-}" == '--remove' ]]; then
  systemctl --user disable --now mtfree.service || true
  rm -f -- "$unit_dir/mtfree.service"
  systemctl --user daemon-reload
  echo 'Service removed. Program and data are preserved.'
  exit
fi
app_dir="$PWD"
if [[ "$app_dir" == *'%'* || "$app_dir" == *'"'* || "$app_dir" == *'\'* || "$app_dir" == *$'\n'* ]]; then
  echo 'Install path must not contain %, double quotes, backslashes or newlines.'; exit 1
fi
[[ -x .venv/bin/python || -x bin/ptfree ]] || { echo 'Run bash install-linux.sh first.'; exit 1; }
mkdir -p -- "$unit_dir"
umask 077
cat > "$unit_dir/mtfree.service" <<UNIT
[Unit]
Description=PTFree automatic torrent downloader
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory="$app_dir"
ExecStart="$app_dir/run-linux.sh"
Restart=on-failure
RestartSec=10
KillMode=control-group
TimeoutStopSec=20
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
Environment=MTFREE_HOST=${MTFREE_HOST:-127.0.0.1}
Environment=MTFREE_PORT=${MTFREE_PORT:-8787}

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now mtfree.service
echo 'Installed. View: systemctl --user status mtfree.service'
echo 'To run before login and after logout, enable lingering once:'
echo "sudo loginctl enable-linger $(id -un)"
