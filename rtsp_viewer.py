#!/usr/bin/env python3
import json
import locale
import math
import os
import re
import socket
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

os.environ["QT_QPA_PLATFORM"] = "xcb"
os.environ.pop("WAYLAND_DISPLAY", None)

import mpv
from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


def config_path():
    override = os.environ.get("RTSP_VIEWER_CONFIG")
    if override:
        return Path(override).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "rtsp-viewer" / "config.json"


CONFIG = config_path()
POLL_MS = 1000
DEAD_TICKS = 5
STUCK_TICKS = 15
LEGACY_RELAY_PREFIX = "relay:"
DEFAULT_WINDOW = [1000, 800]
DEFAULT_SETTINGS = {
    "sidebar": "right",
    "layout": "equal",
    "main_camera": 0,
    "volume_step": 5,
}
RELAY_TIP = (
    "Routes this stream through a local proxy that rewrites the RTSP Transport "
    "header from 'RTP/AVP' to 'RTP/AVP/TCP'.\n"
    "Enable it only if the camera fails to connect or never starts playing "
    "(some cameras answer incorrectly when TCP is requested)."
)

TRANSPORT_RE = re.compile(rb"(?im)^(Transport:[ \t]*RTP/AVP)(;[^\r\n]*interleaved=)")
LENGTH_RE = re.compile(rb"(?i)content-length:[ \t]*(\d+)")


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
    layout = raw.get("layout")
    main = raw.get("main_camera")
    step = raw.get("volume_step")
    return {
        "sidebar": sidebar if sidebar in ("left", "right") else DEFAULT_SETTINGS["sidebar"],
        "layout": layout if layout in ("equal", "main") else DEFAULT_SETTINGS["layout"],
        "main_camera": main if isinstance(main, int) and 0 <= main < count else 0,
        "volume_step": step if isinstance(step, int) and 1 <= step <= 25 else DEFAULT_SETTINGS["volume_step"],
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
                {"url": item[len(LEGACY_RELAY_PREFIX):].strip() if relay else item, "relay": relay}
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


def log_handler(level, prefix, text):
    print(f"[mpv/{prefix}] {text}", end="", flush=True)


class RtspFixProxy:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.local_port = 0
        self.sock = None
        self.conns = set()
        self.lock = threading.Lock()

    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", 0))
            sock.listen(8)
        except OSError:
            sock.close()
            raise
        self.sock = sock
        self.local_port = sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()
        return self.local_port

    def stop(self):
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        with self.lock:
            conns = list(self.conns)
            self.conns.clear()
        for c in conns:
            self._close(c)

    @staticmethod
    def _close(s):
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            s.close()
        except OSError:
            pass

    def _accept(self):
        sock = self.sock
        while sock is not None:
            try:
                client, _ = sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client):
        try:
            camera = socket.create_connection((self.host, self.port), timeout=10)
            camera.settimeout(None)
        except OSError:
            self._close(client)
            return
        for s in (client, camera):
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with self.lock:
            self.conns.update((client, camera))
        threading.Thread(target=self._up, args=(client, camera), daemon=True).start()
        self._down(camera, client)
        self._close(client)
        self._close(camera)
        with self.lock:
            self.conns.discard(client)
            self.conns.discard(camera)

    @staticmethod
    def _up(client, camera):
        try:
            while True:
                data = client.recv(65536)
                if not data:
                    break
                camera.sendall(data)
        except OSError:
            pass
        try:
            camera.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    @staticmethod
    def _down(camera, client):
        buf = b""
        try:
            while True:
                data = camera.recv(65536)
                if not data:
                    break
                buf += data
                while buf:
                    if buf[:1] == b"$":
                        if len(buf) < 4:
                            break
                        n = 4 + int.from_bytes(buf[2:4], "big")
                        if len(buf) < n:
                            break
                        client.sendall(buf[:n])
                        buf = buf[n:]
                    else:
                        i = buf.find(b"\r\n\r\n")
                        if i < 0:
                            break
                        head = buf[: i + 4]
                        m = LENGTH_RE.search(head)
                        clen = int(m.group(1)) if m else 0
                        end = i + 4 + clen
                        if len(buf) < end:
                            break
                        head = TRANSPORT_RE.sub(rb"\1/TCP\2", head)
                        client.sendall(head + buf[i + 4 : end])
                        buf = buf[end:]
        except OSError:
            pass


class CameraWidget(QWidget):
    def __init__(self, on_click, parent=None):
        super().__init__(parent)
        self.on_click = on_click
        self.name = ""
        self.url = ""
        self.relay = False
        self.target = ""
        self.volume = 0
        self.volume_step = DEFAULT_SETTINGS["volume_step"]
        self.state = "no URL"
        self.fatal = ""
        self.idle = 0
        self.was_live = False
        self.player = None
        self.proxy = None

        self.video = QFrame()
        self.video.setStyleSheet("background: black;")
        self.video.setAttribute(Qt.WA_NativeWindow)
        self.video.setMinimumSize(160, 90)
        self.video.installEventFilter(self)

        self.label = QLabel()
        self.label.setStyleSheet(
            "padding: 8px 4px; border-bottom: 1px solid palette(mid);"
        )
        self.label.setWordWrap(True)
        self.label.setAlignment(Qt.AlignTop | Qt.AlignHCenter)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.video, 1)

        self.timer = QTimer(self)
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self._poll)
        self.timer.start()

        self._refresh_label()

    def showEvent(self, ev):
        super().showEvent(ev)
        if self.player is None:
            QTimer.singleShot(0, self._ensure_player)

    def _ensure_player(self):
        if self.player is not None:
            return
        self.player = mpv.MPV(
            wid=str(int(self.video.winId())),
            log_handler=log_handler,
            loglevel="warn",
            vo="gpu",
            gpu_context="x11egl",
            idle=True,
            volume=self.volume,
            hwdec="no",
            cache="no",
            osc=False,
            osd_level=0,
            input_default_bindings=False,
            input_vo_keyboard=False,
            input_cursor=False,
            cursor_autohide="no",
            network_timeout=10,
            demuxer_lavf_o="rtsp_transport=tcp",
            demuxer_lavf_analyzeduration=5,
            demuxer_lavf_probesize=10000000,
        )
        if self.url:
            self.start()

    def _stop_proxy(self):
        if self.proxy is not None:
            self.proxy.stop()
            self.proxy = None

    def _start_proxy(self):
        u = urlsplit(self.url)
        if not u.hostname:
            self.fatal = "invalid URL"
            return
        try:
            proxy = RtspFixProxy(u.hostname, u.port or 554)
            local_port = proxy.start()
        except (ValueError, OSError) as e:
            self.fatal = f"proxy error: {e}"
            return
        self.proxy = proxy
        userinfo = u.netloc.rpartition("@")[0]
        netloc = f"127.0.0.1:{local_port}"
        if userinfo:
            netloc = f"{userinfo}@{netloc}"
        self.target = urlunsplit(u._replace(netloc=netloc))

    def set_name(self, name):
        self.name = name
        self._refresh_label()

    def set_source(self, url, relay):
        url = url.strip()
        if url == self.url and relay == self.relay:
            return
        self.url = url
        self.relay = relay
        self.target = url
        self.fatal = ""
        self._stop_proxy()
        if self.url and self.relay:
            self._start_proxy()
        self.start()

    def start(self):
        self.idle = 0
        self.was_live = False
        if not self.url or self.fatal:
            self.state = self.fatal or "no URL"
            if self.player is not None:
                self.player.stop()
            self._refresh_label()
            return
        self.state = "connecting..."
        self._refresh_label()
        if self.player is not None:
            self.player.play(self.target)

    def _poll(self):
        if not self.url or self.fatal or self.player is None:
            return
        try:
            dead = self.player.idle_active
            waiting = self.player.core_idle
        except Exception:
            return
        if dead:
            self.idle += 1
            self.state = "disconnected" if self.was_live else "connecting..."
            if self.idle >= DEAD_TICKS:
                self.start()
                return
        elif waiting:
            self.idle += 1
            self.state = "loading..." if self.was_live else "connecting..."
            if self.idle >= STUCK_TICKS:
                self.start()
                return
        else:
            self.idle = 0
            self.was_live = True
            self.state = "live"
        self._refresh_label()

    def set_volume(self, value):
        self.volume = max(0, min(100, value))
        if self.player is not None:
            self.player.volume = self.volume
        self._refresh_label()

    def _refresh_label(self):
        icon = "🔇" if self.volume == 0 else "🔊"
        relay = "\nrelay" if self.relay else ""
        self.label.setText(f"{self.name}\n{icon} {self.volume}%\n{self.state}{relay}")

    def shutdown(self):
        self.timer.stop()
        self._stop_proxy()
        if self.player is not None:
            try:
                self.player.terminate()
            except Exception:
                pass
            self.player = None

    def eventFilter(self, obj, ev):
        if obj is self.video:
            if ev.type() == QEvent.MouseButtonPress and ev.button() == Qt.LeftButton:
                self.on_click(self)
                return True
            if ev.type() == QEvent.Wheel:
                dy = ev.angleDelta().y()
                if dy:
                    self.set_volume(
                        self.volume + (self.volume_step if dy > 0 else -self.volume_step)
                    )
                return True
        return super().eventFilter(obj, ev)


class CameraRow(QWidget):
    removed = Signal(object)
    changed = Signal()

    def __init__(self, camera, parent=None):
        super().__init__(parent)
        self.number = QLabel()
        self.number.setMinimumWidth(18)
        self.name = QLineEdit(camera["name"])
        self.name.setPlaceholderText("Name (optional)")
        self.name.setFixedWidth(150)
        self.url = QLineEdit(camera["url"])
        self.url.setPlaceholderText("rtsp://user:password@host:port/path")
        self.relay = QCheckBox("Relay")
        self.relay.setChecked(camera["relay"])
        self.relay.setToolTip(RELAY_TIP)
        self.remove = QPushButton("✕")
        self.remove.setFixedWidth(32)
        self.remove.setToolTip("Remove camera")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.number)
        layout.addWidget(self.name)
        layout.addWidget(self.url, 1)
        layout.addWidget(self.relay)
        layout.addWidget(self.remove)

        self.name.textChanged.connect(lambda _: self.changed.emit())
        self.remove.clicked.connect(lambda: self.removed.emit(self))

    def data(self):
        return {
            "name": self.name.text().strip(),
            "url": self.url.text().strip(),
            "relay": self.relay.isChecked(),
        }


class SettingsDialog(QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(780)
        self.rows = []
        settings = config["settings"]

        self.sidebar = QComboBox()
        self.sidebar.addItem("Left", "left")
        self.sidebar.addItem("Right", "right")
        self.sidebar.setCurrentIndex(self.sidebar.findData(settings["sidebar"]))

        self.layout_mode = QComboBox()
        self.layout_mode.addItem("Equal size", "equal")
        self.layout_mode.addItem("Main camera", "main")
        self.layout_mode.setCurrentIndex(self.layout_mode.findData(settings["layout"]))
        self.layout_mode.currentIndexChanged.connect(lambda _: self._refresh())

        self.main = QComboBox()

        self.step = QSpinBox()
        self.step.setRange(1, 25)
        self.step.setSuffix("%")
        self.step.setValue(settings["volume_step"])
        self.step.setToolTip("Volume change per mouse wheel notch")

        self.rows_layout = QVBoxLayout()
        self.rows_layout.setContentsMargins(6, 6, 6, 6)
        self.rows_layout.setSpacing(6)
        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.addLayout(self.rows_layout)
        holder_layout.addStretch()
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(holder)
        self.scroll.setMinimumHeight(200)

        add = QPushButton("+ Add camera")
        add.clicked.connect(self._add_clicked)

        cams_box = QGroupBox("Cameras")
        cams_layout = QVBoxLayout(cams_box)
        cams_layout.addWidget(self.scroll)
        cams_layout.addWidget(add, 0, Qt.AlignLeft)

        display_box = QGroupBox("Display")
        form = QFormLayout(display_box)
        form.addRow("Sidebar position", self.sidebar)
        form.addRow("Layout", self.layout_mode)
        form.addRow("Main camera", self.main)
        form.addRow("Volume step", self.step)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(cams_box, 1)
        layout.addWidget(display_box)
        layout.addWidget(buttons)

        for cam in config["cameras"]:
            self._add_row(cam)
        self._refresh(settings["main_camera"])

    def _add_row(self, camera):
        row = CameraRow(camera)
        row.removed.connect(self._remove_row)
        row.changed.connect(self._refresh)
        self.rows.append(row)
        self.rows_layout.addWidget(row)
        self._refresh()
        return row

    def _add_clicked(self):
        row = self._add_row(new_camera())
        row.url.setFocus()
        bar = self.scroll.verticalScrollBar()
        QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))

    def _remove_row(self, row):
        if len(self.rows) <= 1:
            return
        self.rows.remove(row)
        self.rows_layout.removeWidget(row)
        row.hide()
        row.deleteLater()
        self._refresh()

    def _refresh(self, keep=None):
        count = len(self.rows)
        for i, row in enumerate(self.rows, 1):
            row.number.setText(str(i))
            row.remove.setEnabled(count > 1)
        current = self.main.currentIndex() if keep is None else keep
        self.main.blockSignals(True)
        self.main.clear()
        for i, row in enumerate(self.rows, 1):
            self.main.addItem(f"{i} - {row.name.text().strip() or f'Camera {i}'}")
        self.main.setCurrentIndex(max(0, min(current, count - 1)))
        self.main.blockSignals(False)
        self.main.setEnabled(self.layout_mode.currentData() == "main")

    def get_config(self):
        return {
            "cameras": [row.data() for row in self.rows],
            "settings": {
                "sidebar": self.sidebar.currentData(),
                "layout": self.layout_mode.currentData(),
                "main_camera": max(0, self.main.currentIndex()),
                "volume_step": self.step.value(),
            },
        }


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("RTSP Viewer")
        self.config = load_config()
        self.resize(*self.config["window"])
        self.focused = None
        self.cams = []

        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(0)

        button = QPushButton("⚙ Settings")
        button.clicked.connect(self.open_settings)

        self.labels_layout = QVBoxLayout()
        self.labels_layout.setContentsMargins(0, 0, 0, 0)
        self.labels_layout.setSpacing(0)
        self.labels_layout.addStretch()
        labels_host = QWidget()
        labels_host.setLayout(self.labels_layout)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(labels_host)

        self.sidebar = QWidget()
        self.sidebar.setFixedWidth(130)
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(0)
        side.addWidget(button)
        side.addWidget(scroll, 1)

        central = QWidget()
        self.body = QHBoxLayout(central)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(0)
        self.setCentralWidget(central)

        QShortcut(QKeySequence(Qt.Key_F11), self).activated.connect(self.toggle_fullscreen)

        self.apply_config()
        if not any(c["url"] for c in self.config["cameras"]):
            QTimer.singleShot(0, self.open_settings)

    def _add_camera(self):
        cam = CameraWidget(self.toggle_focus, self.grid_host)
        self.cams.append(cam)
        self.labels_layout.insertWidget(self.labels_layout.count() - 1, cam.label)

    def _remove_camera(self, cam):
        cam.shutdown()
        self.grid.removeWidget(cam)
        self.labels_layout.removeWidget(cam.label)
        cam.label.hide()
        cam.label.deleteLater()
        cam.hide()
        cam.deleteLater()

    def apply_config(self):
        cameras = self.config["cameras"]
        settings = self.config["settings"]
        self.focused = None
        while len(self.cams) > len(cameras):
            self._remove_camera(self.cams.pop())
        while len(self.cams) < len(cameras):
            self._add_camera()
        for i, (cam, data) in enumerate(zip(self.cams, cameras), 1):
            cam.volume_step = settings["volume_step"]
            cam.set_name(data["name"] or f"Camera {i}")
            cam.set_source(data["url"], data["relay"])
        self.apply_sidebar()
        self.apply_layout()

    def apply_sidebar(self):
        self.body.removeWidget(self.sidebar)
        self.body.removeWidget(self.grid_host)
        if self.config["settings"]["sidebar"] == "left":
            self.body.addWidget(self.sidebar)
            self.body.addWidget(self.grid_host, 1)
        else:
            self.body.addWidget(self.grid_host, 1)
            self.body.addWidget(self.sidebar)

    def apply_layout(self):
        for cam in self.cams:
            self.grid.removeWidget(cam)
        for i in range(self.grid.rowCount()):
            self.grid.setRowStretch(i, 0)
        for i in range(self.grid.columnCount()):
            self.grid.setColumnStretch(i, 0)

        settings = self.config["settings"]
        count = len(self.cams)

        if self.focused is not None:
            self.grid.addWidget(self.focused, 0, 0)
            self.grid.setRowStretch(0, 1)
            self.grid.setColumnStretch(0, 1)
        elif settings["layout"] == "main" and count > 1:
            main = self.cams[min(settings["main_camera"], count - 1)]
            others = [c for c in self.cams if c is not main]
            self.grid.addWidget(main, 0, 0, 1, len(others))
            for j, cam in enumerate(others):
                self.grid.addWidget(cam, 1, j)
                self.grid.setColumnStretch(j, 1)
            self.grid.setRowStretch(0, 3)
            self.grid.setRowStretch(1, 1)
        else:
            cols = max(1, math.ceil(math.sqrt(count)))
            rows = max(1, math.ceil(count / cols))
            for i, cam in enumerate(self.cams):
                self.grid.addWidget(cam, i // cols, i % cols)
            for r in range(rows):
                self.grid.setRowStretch(r, 1)
            for c in range(cols):
                self.grid.setColumnStretch(c, 1)

        for cam in self.cams:
            cam.setVisible(self.focused is None or cam is self.focused)

    def toggle_focus(self, cam):
        self.focused = None if self.focused is cam else cam
        self.apply_layout()

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def open_settings(self):
        dialog = SettingsDialog(self.config, self)
        if dialog.exec():
            self.config.update(dialog.get_config())
            save_config(self.config)
            self.apply_config()

    def closeEvent(self, ev):
        for cam in self.cams:
            cam.shutdown()
        if not (self.isFullScreen() or self.isMaximized()):
            self.config["window"] = [self.width(), self.height()]
        save_config(self.config)
        super().closeEvent(ev)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    locale.setlocale(locale.LC_NUMERIC, "C")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
