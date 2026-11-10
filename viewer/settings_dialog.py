from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .config import new_camera

RELAY_TIP = (
    "Routes this stream through a local proxy that rewrites the RTSP Transport "
    "header from 'RTP/AVP' to 'RTP/AVP/TCP'.\n"
    "Enable it only if the camera fails to connect or never starts playing "
    "(some cameras answer incorrectly when TCP is requested)."
)


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


class RadioGroup(QWidget):
    def __init__(self, options, current, parent=None):
        super().__init__(parent)
        self.group = QButtonGroup(self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for label, value in options:
            button = QRadioButton(label)
            button.setProperty("value", value)
            button.setChecked(value == current)
            self.group.addButton(button)
            layout.addWidget(button)
        layout.addStretch()

    def value(self):
        return self.group.checkedButton().property("value")


class SettingsDialog(QDialog):
    applied = Signal(dict)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(780)
        self.rows = []
        settings = config["settings"]

        self.sidebar = RadioGroup(
            [("Left", "left"), ("Right", "right")], settings["sidebar"]
        )
        self.sidebar_v = RadioGroup(
            [("Top", "top"), ("Bottom", "bottom")], settings["sidebar_v"]
        )
        self.layout_mode = RadioGroup(
            [("Equal size", "equal"), ("Main camera", "main")], settings["layout"]
        )
        self.layout_mode.group.buttonClicked.connect(lambda _: self._refresh())

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

        position = QWidget()
        position_layout = QVBoxLayout(position)
        position_layout.setContentsMargins(0, 0, 0, 0)
        position_layout.addWidget(self.sidebar)
        position_layout.addWidget(self.sidebar_v)

        display_box = QGroupBox("Display")
        form = QFormLayout(display_box)
        form.addRow("Sidebar position", position)
        form.addRow("Layout", self.layout_mode)
        form.addRow("Main camera", self.main)
        form.addRow("Volume step", self.step)

        buttons = QDialogButtonBox(QDialogButtonBox.Apply | QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Apply).clicked.connect(self._apply)
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
        self.main.setEnabled(self.layout_mode.value() == "main")

    def _apply(self):
        self.applied.emit(self.get_config())

    def get_config(self):
        return {
            "cameras": [row.data() for row in self.rows],
            "settings": {
                "sidebar": self.sidebar.value(),
                "sidebar_v": self.sidebar_v.value(),
                "layout": self.layout_mode.value(),
                "main_camera": max(0, self.main.currentIndex()),
                "volume_step": self.step.value(),
            },
        }
