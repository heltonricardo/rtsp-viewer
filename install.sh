#!/usr/bin/env bash
set -e
cd "$(dirname "$(readlink -f "$0")")"
DIR="$(pwd)"

if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get install -y python3-venv libxcb-cursor0
    sudo apt-get install -y libmpv2 || sudo apt-get install -y libmpv1
fi

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

chmod +x run.sh
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/rtsp-viewer.desktop" << DESKTOP
[Desktop Entry]
Type=Application
Name=RTSP Viewer
Comment=Multi-camera RTSP viewer
Exec=$DIR/run.sh
Icon=camera-video
Terminal=false
Categories=AudioVideo;Video;
DESKTOP

echo "Installed. Launch from the application menu or run ./run.sh"
