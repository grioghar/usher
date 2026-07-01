#!/usr/bin/env bash
# usher installer for an LXC (or any Debian/Ubuntu host). Run from the repo root:
#   sudo lxc/install.sh
set -euo pipefail
PREFIX=/opt/usher
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
apt-get update -qq
apt-get install -y -qq python3 python3-venv
mkdir -p "$PREFIX" /etc/usher
install -m0644 "$ROOT/host/director.py"        "$PREFIX/director.py"
install -m0644 "$ROOT/src/plex_director.wasm"  "$PREFIX/plex_director.wasm"
[ -f /etc/usher/usher.yaml ] || install -m0644 "$ROOT/config/usher.example.yaml" /etc/usher/usher.yaml
python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install -q wasmtime pyyaml
cat >/etc/systemd/system/usher.service <<EOF
[Unit]
Description=usher — N+1 Plex load director
After=network-online.target
Wants=network-online.target
[Service]
Environment=USHER_CONFIG=/etc/usher/usher.yaml
Environment=USHER_WASM=$PREFIX/plex_director.wasm
# put your token here or in an EnvironmentFile:  Environment=PLEX_TOKEN=xxxx
ExecStart=$PREFIX/venv/bin/python -u $PREFIX/director.py
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now usher
echo "usher installed. Edit /etc/usher/usher.yaml (+ set PLEX_TOKEN), then: systemctl restart usher"
