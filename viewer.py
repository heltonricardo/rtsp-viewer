#!/usr/bin/env python3
"""Visualizador de 3 câmeras RTSP (com áudio) - PySide6 / Qt Multimedia.

- Botão "URLs" abre o diálogo para colar as 3 URLs completas (user:senha@ip:porta/path)
- Clique em uma câmera: ocupa a janela toda (não é fullscreen). Clique de novo: volta às 3.
- Scroll sobre a câmera: controla o volume (5% por passo). Padrão: 0%.
"""
import json
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer, QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QPushButton, QVBoxLayout, QWidget,
)

CONFIG = Path.home() / ".config" / "rtsp-viewer" / "config.json"
VOLUME_STEP = 5  # % por "clique" da roda do mouse
RETRY_MS = 5000  # reconecta sozinho se cair


def load_urls():
    try:
        urls = json.loads(CONFIG.read_text())["urls"]
        return (urls + ["", "", ""])[:3]
    except Exception:
        return ["", "", ""]


def save_urls(urls):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps({"urls": urls}, indent=2))
    CONFIG.chmod(0o600)  # tem senha dentro


class CameraWidget(QWidget):
    def __init__(self, name, on_click):
        super().__init__()
        self.name = name
        self.on_click = on_click
        self.url = ""
        self.volume = 0  # 0..100
        self.state = "sem URL"

        self.video = QVideoWidget()
        self.video.setStyleSheet("background: black;")
        self.video.setMinimumSize(160, 90)
        self.video.installEventFilter(self)

        self.label = QLabel()
        self.label.setStyleSheet("padding: 2px 6px;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)
        layout.addWidget(self.video, 1)
        layout.addWidget(self.label)

        self.audio = QAudioOutput()
        self.audio.setVolume(0.0)
        self.player = QMediaPlayer()
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.player.errorOccurred.connect(self._on_error)
        self.player.mediaStatusChanged.connect(self._on_status)

        self.retry = QTimer(self)
        self.retry.setSingleShot(True)
        self.retry.setInterval(RETRY_MS)
        self.retry.timeout.connect(self.start)

        self._refresh_label()

    # ---- reprodução ----
    def set_url(self, url):
        self.url = url.strip()
        self.start()

    def start(self):
        self.retry.stop()
        self.player.stop()
        if not self.url:
            self.state = "sem URL"
            self._refresh_label()
            return
        self.state = "conectando..."
        self._refresh_label()
        self.player.setSource(QUrl(self.url))
        self.player.play()

    def _on_status(self, status):
        S = QMediaPlayer.MediaStatus
        if status in (S.LoadedMedia, S.BufferedMedia):
            self.state = "ao vivo"
        elif status in (S.LoadingMedia, S.StalledMedia, S.BufferingMedia):
            self.state = "carregando..."
        elif status == S.EndOfMedia:
            self.state = "desconectado"
            self.retry.start()
        self._refresh_label()

    def _on_error(self, error, message):
        self.state = f"erro: {message}"
        self._refresh_label()
        if self.url:
            self.retry.start()

    # ---- volume / mouse ----
    def set_volume(self, value):
        self.volume = max(0, min(100, value))
        self.audio.setVolume(self.volume / 100)
        self._refresh_label()

    def _refresh_label(self):
        icon = "🔇" if self.volume == 0 else "🔊"
        self.label.setText(f"{self.name}  ·  {icon} {self.volume}%  ·  {self.state}")

    def eventFilter(self, obj, ev):
        if obj is self.video:
            if ev.type() == QEvent.MouseButtonPress and ev.button() == Qt.LeftButton:
                self.on_click(self)
                return True
            if ev.type() == QEvent.Wheel:
                dy = ev.angleDelta().y()
                if dy:
                    self.set_volume(self.volume + (VOLUME_STEP if dy > 0 else -VOLUME_STEP))
                return True
        return super().eventFilter(obj, ev)


class UrlDialog(QDialog):
    def __init__(self, urls, parent=None):
        super().__init__(parent)
        self.setWindowTitle("URLs das câmeras")
        self.setMinimumWidth(650)
        form = QFormLayout()
        self.edits = []
        for i, u in enumerate(urls, 1):
            e = QLineEdit(u)
            e.setPlaceholderText("rtsp://user:senha@ip:porta/path")
            form.addRow(f"Câmera {i}", e)
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
        self.setWindowTitle("Câmeras")
        self.resize(1400, 400)
        self.focused = None

        self.cams = [CameraWidget(f"Câmera {i}", self.toggle_focus) for i in (1, 2, 3)]

        btn = QPushButton("⚙ URLs")
        btn.clicked.connect(self.edit_urls)
        top = QHBoxLayout()
        top.addWidget(btn)
        top.addStretch()

        row = QHBoxLayout()
        for c in self.cams:
            row.addWidget(c, 1)

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addLayout(top)
        lay.addLayout(row, 1)
        self.setCentralWidget(central)

        urls = load_urls()
        for c, u in zip(self.cams, urls):
            c.set_url(u)
        if not any(urls):
            QTimer.singleShot(0, self.edit_urls)

    def toggle_focus(self, cam):
        if self.focused is cam:
            self.focused = None
            for c in self.cams:
                c.setVisible(True)
        else:
            self.focused = cam
            for c in self.cams:
                c.setVisible(c is cam)

    def edit_urls(self):
        dlg = UrlDialog([c.url for c in self.cams], self)
        if dlg.exec():
            urls = dlg.urls()
            save_urls(urls)
            for c, u in zip(self.cams, urls):
                c.set_url(u)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
