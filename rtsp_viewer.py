#!/usr/bin/env python3
import locale
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "xcb"
os.environ.pop("WAYLAND_DISPLAY", None)

from PySide6.QtWidgets import QApplication

from viewer.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    locale.setlocale(locale.LC_NUMERIC, "C")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
