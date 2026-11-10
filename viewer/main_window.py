import math

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .camera import CameraWidget
from .config import load_config, save_config
from .settings_dialog import SettingsDialog


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
        self.labels_layout.addStretch()
        labels_host = QWidget()
        labels_host.setLayout(self.labels_layout)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(labels_host)

        self.settings_button = button
        self.labels_scroll = scroll
        self.sidebar = QWidget()
        self.sidebar.setFixedWidth(100)
        self.side_layout = QVBoxLayout(self.sidebar)
        self.side_layout.setContentsMargins(0, 0, 0, 0)
        self.side_layout.setSpacing(0)

        central = QWidget()
        self.body = QHBoxLayout(central)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(0)
        self.setCentralWidget(central)

        QShortcut(QKeySequence(Qt.Key_F11), self).activated.connect(
            self.toggle_fullscreen
        )

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
        settings = self.config["settings"]
        self.body.removeWidget(self.sidebar)
        self.body.removeWidget(self.grid_host)
        if settings["sidebar"] == "left":
            self.body.addWidget(self.sidebar)
            self.body.addWidget(self.grid_host, 1)
        else:
            self.body.addWidget(self.grid_host, 1)
            self.body.addWidget(self.sidebar)

        top = settings["sidebar_v"] == "top"
        self.side_layout.removeWidget(self.settings_button)
        self.side_layout.removeWidget(self.labels_scroll)
        if top:
            self.side_layout.addWidget(self.settings_button)
            self.side_layout.addWidget(self.labels_scroll, 1)
        else:
            self.side_layout.addWidget(self.labels_scroll, 1)
            self.side_layout.addWidget(self.settings_button)
        self.labels_layout.setStretch(0, 0 if top else 1)
        self.labels_layout.setStretch(self.labels_layout.count() - 1, 1 if top else 0)

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
        for cam in self.cams:
            cam.set_focused(cam is self.focused)

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
        dialog.applied.connect(self.apply_settings)
        dialog.exec()

    def apply_settings(self, new_config):
        self.config.update(new_config)
        save_config(self.config)
        self.apply_config()

    def closeEvent(self, ev):
        for cam in self.cams:
            cam.shutdown()
        if not (self.isFullScreen() or self.isMaximized()):
            self.config["window"] = [self.width(), self.height()]
        save_config(self.config)
        super().closeEvent(ev)
