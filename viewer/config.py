import json
import os
import sys
from pathlib import Path


def config_path():
    override = os.environ.get("RTSP_VIEWER_CONFIG")
    if override:
        return Path(override).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "rtsp-viewer" / "config.json"


CONFIG = config_path()
LEGACY_RELAY_PREFIX = "relay:"
DEFAULT_WINDOW = [1000, 800]
DEFAULT_SETTINGS = {
    "sidebar": "right",
    "sidebar_v": "top",
    "layout": "equal",
    "main_camera": 0,
    "volume_step": 5,
}


def new_camera():
    return {"name": "", "url": "", "relay": False}


def sanitize_camera(raw):
    if not isinstance(raw, dict):
        return new_camera()
    return {
        "name": str(raw.get("name", "")).strip(),
        "url": str(raw.get("url", "")).strip(),
        "relay": bool(raw.get("relay", False)),
    }


def sanitize_settings(raw, count):
    raw = raw if isinstance(raw, dict) else {}
    sidebar = raw.get("sidebar")
    sidebar_v = raw.get("sidebar_v")
    layout = raw.get("layout")
    main = raw.get("main_camera")
    step = raw.get("volume_step")
    return {
        "sidebar": (
            sidebar if sidebar in ("left", "right") else DEFAULT_SETTINGS["sidebar"]
        ),
        "sidebar_v": (
            sidebar_v
            if sidebar_v in ("top", "bottom")
            else DEFAULT_SETTINGS["sidebar_v"]
        ),
        "layout": layout if layout in ("equal", "main") else DEFAULT_SETTINGS["layout"],
        "main_camera": main if isinstance(main, int) and 0 <= main < count else 0,
        "volume_step": (
            step
            if isinstance(step, int) and 1 <= step <= 25
            else DEFAULT_SETTINGS["volume_step"]
        ),
    }


def load_config():
    try:
        data = json.loads(CONFIG.read_text())
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    raw = data.get("cameras")
    if raw is None and isinstance(data.get("urls"), list):
        raw = []
        for item in data["urls"]:
            item = str(item).strip()
            if not item:
                continue
            relay = item.startswith(LEGACY_RELAY_PREFIX)
            raw.append(
                {
                    "url": item[len(LEGACY_RELAY_PREFIX) :].strip() if relay else item,
                    "relay": relay,
                }
            )
    cameras = [sanitize_camera(c) for c in raw] if isinstance(raw, list) else []
    if not cameras:
        cameras = [new_camera()]
    size = data.get("window")
    if not (
        isinstance(size, list)
        and len(size) == 2
        and all(isinstance(v, int) and v > 0 for v in size)
    ):
        size = list(DEFAULT_WINDOW)
    return {
        "cameras": cameras,
        "settings": sanitize_settings(data.get("settings"), len(cameras)),
        "window": size,
    }


def save_config(config):
    try:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG.with_suffix(".tmp")
        tmp.write_text(json.dumps(config, indent=2))
        tmp.chmod(0o600)
        tmp.replace(CONFIG)
    except OSError as e:
        print(f"could not save config: {e}", file=sys.stderr)
