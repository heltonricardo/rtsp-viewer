#!/usr/bin/env python3
import json
import locale
import os
import sys
from pathlib import Path

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


class CameraWidget(QWidget):
    def __init__(self, name, on_click):
        super().__init__()
        self.name = name
        self.on_click = on_click
        self.url = ""
        self.volume = 0
        self.state = "no URL"
        self.idle = 0
        self.was_live = False
        self.player = None

        self.video = QFrame()
        self.video.setStyleSheet("background: black;")
        self.video.setAttribute(Qt.WA_NativeWindow)
        self.video.setMinimumSize(160, 90)
        self.video.installEventFilter(self)

        self.label = QLabel()
        self.label.setStyleSheet("padding: 2px 6px;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)
        layout.addWidget(self.video, 1)
        layout.addWidget(self.label)

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

    def set_url(self, url):
        self.url = url.strip()
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
            self.player.play(self.url)

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
        self.label.setText(f"{self.name}  ·  {icon} {self.volume}%  ·  {self.state}")

    def shutdown(self):
        self.timer.stop()
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
        self.setMinimumWidth(650)
        form = QFormLayout()
        self.edits = []
        for i, u in enumerate(urls, 1):
            e = QLineEdit(u)
            e.setPlaceholderText("rtsp://user:password@host:port/path")
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

        self.cams = [CameraWidget(f"Camera {i}", self.toggle_focus) for i in (1, 2, 3)]

        button = QPushButton("⚙ URLs")
        button.clicked.connect(self.edit_urls)
        top = QHBoxLayout()
        top.addWidget(button)
        top.addStretch()

        self.grid = QGridLayout()
        self.grid.setContentsMargins(0, 0, 0, 0)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(top)
        layout.addLayout(self.grid, 1)
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
