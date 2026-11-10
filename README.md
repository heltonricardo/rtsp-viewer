# RTSP Viewer

Lightweight multi-camera RTSP viewer for Linux, built with PySide6 and libmpv.

## Features

- Any number of cameras, added from the settings dialog
- Single camera fills the whole window by default
- Layouts: all cameras equal size, or one main camera with the others below
- Click a camera to maximize it, click again to go back
- Mouse wheel over a camera changes its volume (starts muted)
- Compact sidebar with per-camera volume and connection status, placed left or right and aligned top or bottom
- Automatic reconnection when a stream drops or stalls
- Optional per-camera relay proxy for cameras with a faulty RTSP `Transport` header
- Settings apply instantly, no restart needed
- `F11` toggles fullscreen

## Tested on

Kubuntu (Wayland session). The app runs through XWayland (`xcb`) because libmpv embeds its video into a native X11 window.

## Requirements

- Python 3.9+
- libmpv
- `libxcb-cursor0` (required by Qt 6.5+ xcb plugin)
- `python3-venv`

On Ubuntu/Kubuntu/Debian:

```bash
sudo apt install python3-venv libxcb-cursor0 libmpv2
```

On Ubuntu 22.04 or older, use `libmpv1` instead of `libmpv2`.

## Install

```bash
git clone https://github.com/<user>/rtsp-viewer.git
cd rtsp-viewer
./install.sh
```

`install.sh` installs the system packages (apt only), creates a `.venv`, installs the Python dependencies, and adds an "RTSP Viewer" entry to the application menu.

Manual install:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python rtsp_viewer.py
```

## Run

```bash
./run.sh
```

or launch **RTSP Viewer** from the application menu.

## Usage

Open **⚙ Settings** in the sidebar. **Apply** saves and applies the changes without closing the dialog; **Close** dismisses it.

| Setting | Description |
|---|---|
| Cameras | One row per camera: optional name, RTSP URL, relay checkbox. Use **+ Add camera** to add more and **✕** to remove. |
| Sidebar position | Side (Left/Right) and alignment (Top/Bottom). The Settings button stays on the same end as the camera info. |
| Layout | **Equal size** places all cameras in a grid. **Main camera** shows one large camera with the rest in a row below. |
| Main camera | Which camera is the large one (Main camera layout only). |
| Volume step | Volume change per mouse wheel notch. |

URL format:

```
rtsp://user:password@host:554/path
```

### Relay

Some cameras reply to the TCP setup with `Transport: RTP/AVP;...` instead of `RTP/AVP/TCP;...`, which makes the stream fail. With **Relay** enabled, the camera is accessed through a local proxy (random port on `127.0.0.1`) that fixes that header. Enable it only if a camera does not connect without it.

## Configuration file

Settings and URLs are stored in:

```
~/.config/rtsp-viewer/config.json
```

(or `$XDG_CONFIG_HOME/rtsp-viewer/config.json`). Override the location with the `RTSP_VIEWER_CONFIG` environment variable.

The file is saved with `600` permissions, but **credentials are stored in plain text**. Do not share or commit it.

Configs from older versions (`urls` list with a `relay:` prefix) are migrated automatically.

## Troubleshooting

| Problem | Fix |
|---|---|
| `OSError: Cannot find libmpv` | Install `libmpv2` (or `libmpv1`) |
| `Could not load the Qt platform plugin "xcb"` | Install `libxcb-cursor0` |
| Camera stays on `connecting...` | Check the URL, then try enabling **Relay** |
| Black screen with audio only | Check the `[mpv/...]` messages printed in the terminal |

## License

[MIT](LICENSE)
