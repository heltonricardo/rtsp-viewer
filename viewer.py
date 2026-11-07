#!/usr/bin/env python3
import json
import locale
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
from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

CONFIG = Path.home() / ".config" / "rtsp-viewer" / "config.json"
VOLUME_STEP = 5
POLL_MS = 1000
DEAD_TICKS = 5
STUCK_TICKS = 15
RELAY_PREFIX = "relay:"
RELAY_BASE_PORT = 18080

TRANSPORT_RE = re.compile(rb"(?im)^(Transport:[ \t]*RTP/AVP)(;[^\r\n]*interleaved=)")
LENGTH_RE = re.compile(rb"(?i)content-length:[ \t]*(\d+)")


def load_urls():
    try:
        urls = json.loads(CONFIG.read_text())["urls"]
        return (urls + ["", "", ""])[:3]
    except Exception:
        return ["", "", ""]


def save_urls(urls):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps({"urls": urls}, indent=2))
    CONFIG.chmod(0o600)


def log_handler(level, prefix, text):
    print(f"[mpv/{prefix}] {text}", end="", flush=True)


class RtspFixProxy:
    def __init__(self, local_port, host, port):
        self.local_port = local_port
        self.host = host
        self.port = port
        self.sock = None
        self.conns = set()
        self.lock = threading.Lock()

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", self.local_port))
        self.sock.listen(8)
        threading.Thread(target=self._accept, daemon=True).start()

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
    def __init__(self, name, on_click, port):
        super().__init__()
        self.name = name
        self.on_click = on_click
        self.port = port
        self.url = ""
        self.target = ""
        self.volume = 0
        self.state = "no URL"
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
            volume=0,
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
        real = self.url[len(RELAY_PREFIX) :].strip()
        u = urlsplit(real)
        userinfo = u.netloc.rpartition("@")[0]
        netloc = f"127.0.0.1:{self.port}"
        if userinfo:
            netloc = f"{userinfo}@{netloc}"
        self.target = urlunsplit(u._replace(netloc=netloc))
        self.proxy = RtspFixProxy(self.port, u.hostname, u.port or 554)
        try:
            self.proxy.start()
        except OSError as e:
            self.proxy = None
            self.state = f"proxy error: {e}"

    def set_url(self, url):
        self.url = url.strip()
        self.target = self.url
        self._stop_proxy()
        if self.url.startswith(RELAY_PREFIX):
            self._start_proxy()
        self.start()

    def start(self):
        self.idle = 0
        self.was_live = False
        if not self.url:
            self.state = "no URL"
            if self.player is not None:
                self.player.stop()
            self._refresh_label()
            return
        self.state = "connecting..."
        self._refresh_label()
        if self.player is not None:
            self.player.play(self.target)

    def _poll(self):
        if not self.url or self.player is None:
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
        relay = "\nproxy" if self.url.startswith(RELAY_PREFIX) else ""
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
                        self.volume + (VOLUME_STEP if dy > 0 else -VOLUME_STEP)
                    )
                return True
        return super().eventFilter(obj, ev)


class UrlDialog(QDialog):
    def __init__(self, urls, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Camera URLs")
        self.setMinimumWidth(700)
        form = QFormLayout()
        self.edits = []
        for i, u in enumerate(urls, 1):
            e = QLineEdit(u)
            e.setPlaceholderText("[relay:]rtsp://user:password@host:port/path")
            form.addRow(f"Camera {i}", e)
            self.edits.append(e)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def urls(self):
        return [e.text().strip() for e in self.edits]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Cameras")
        self.resize(1000, 800)
        self.focused = None

        self.cams = [
            CameraWidget(f"Camera {i}", self.toggle_focus, RELAY_BASE_PORT + i)
            for i in (1, 2, 3)
        ]

        button = QPushButton("⚙ URLs")
        button.clicked.connect(self.edit_urls)
        top = QHBoxLayout()
        top.setContentsMargins(4, 4, 4, 4)
        top.addWidget(button)
        top.addStretch()

        self.grid = QGridLayout()
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(0)

        sidebar = QWidget()
        sidebar.setFixedWidth(110)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(0)
        side.addStretch()
        for cam in self.cams:
            side.addWidget(cam.label)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addLayout(self.grid, 1)
        body.addWidget(sidebar)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(top)
        layout.addLayout(body, 1)
        self.setCentralWidget(central)

        self.apply_layout()

        urls = load_urls()
        for cam, url in zip(self.cams, urls):
            cam.set_url(url)
        if not any(urls):
            QTimer.singleShot(0, self.edit_urls)

    def apply_layout(self):
        for cam in self.cams:
            self.grid.removeWidget(cam)
        for i in range(2):
            self.grid.setRowStretch(i, 0)
            self.grid.setColumnStretch(i, 0)

        if self.focused:
            for cam in self.cams:
                cam.setVisible(cam is self.focused)
            self.grid.addWidget(self.focused, 0, 0)
            self.grid.setRowStretch(0, 1)
            self.grid.setColumnStretch(0, 1)
        else:
            for cam in self.cams:
                cam.setVisible(True)
            main, left, right = self.cams
            self.grid.addWidget(main, 0, 0, 1, 2)
            self.grid.addWidget(left, 1, 0)
            self.grid.addWidget(right, 1, 1)
            self.grid.setRowStretch(0, 3)
            self.grid.setRowStretch(1, 1)
            self.grid.setColumnStretch(0, 1)
            self.grid.setColumnStretch(1, 1)

    def toggle_focus(self, cam):
        self.focused = None if self.focused is cam else cam
        self.apply_layout()

    def edit_urls(self):
        dialog = UrlDialog([c.url for c in self.cams], self)
        if dialog.exec():
            urls = dialog.urls()
            save_urls(urls)
            for cam, url in zip(self.cams, urls):
                cam.set_url(url)

    def closeEvent(self, ev):
        for cam in self.cams:
            cam.shutdown()
        super().closeEvent(ev)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    locale.setlocale(locale.LC_NUMERIC, "C")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
