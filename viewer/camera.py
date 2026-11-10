from urllib.parse import urlsplit, urlunsplit

import mpv
from PySide6.QtCore import QEvent, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

from .config import DEFAULT_SETTINGS
from .proxy import RtspFixProxy

POLL_MS = 1000
DEAD_TICKS = 5
STUCK_TICKS = 15
CATCHUP_START = 1.0
CATCHUP_STOP = 0.3
CATCHUP_SPEED = 1.15
LAG_RESTART = 15.0


def log_handler(level, prefix, text):
    print(f"[mpv/{prefix}] {text}", end="", flush=True)


class SelectionBox(QWidget):
    def __init__(self):
        super().__init__(
            None,
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowTransparentForInput
            | Qt.X11BypassWindowManagerHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255), 2))
        p.drawRect(self.rect().adjusted(1, 1, -1, -1))
        pen = QPen(QColor(0, 0, 0), 1, Qt.DashLine)
        p.setPen(pen)
        p.drawRect(self.rect().adjusted(1, 1, -1, -1))


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
        self.focused = False
        self.zoomed = False
        self.press_pos = None
        self.dragging = False
        self.band = SelectionBox()

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
            cache_pause="no",
            demuxer_readahead_secs=0.5,
            demuxer_max_bytes="2MiB",
            audio_buffer=0.1,
            video_latency_hacks="yes",
            osc=False,
            osd_level=0,
            input_default_bindings=False,
            input_vo_keyboard=False,
            input_cursor=False,
            cursor_autohide="no",
            network_timeout=10,
            demuxer_lavf_o="rtsp_transport=tcp,fflags=+nobuffer,flags=+low_delay",
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
        self.reset_zoom()
        if self.player is not None:
            try:
                self.player.speed = 1.0
            except Exception:
                pass
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

    def _catch_up(self):
        try:
            pos = self.player.time_pos
            end = self.player.demuxer_cache_time
            speed = self.player.speed
        except Exception:
            return
        if pos is None or end is None:
            return
        lag = end - pos
        if lag > LAG_RESTART:
            self.start()
        elif lag > CATCHUP_START and speed == 1.0:
            self.player.speed = CATCHUP_SPEED
        elif lag < CATCHUP_STOP and speed != 1.0:
            self.player.speed = 1.0

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
            self._catch_up()
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

    def set_focused(self, value):
        self.focused = value
        if not value:
            self._cancel_drag()
            self.reset_zoom()

    def _cancel_drag(self):
        self.press_pos = None
        self.dragging = False
        self.band.hide()

    def reset_zoom(self):
        if not self.zoomed:
            return
        self.zoomed = False
        if self.player is not None:
            try:
                self.player["video-crop"] = ""
            except Exception:
                pass

    def _selection(self, pos):
        return QRect(self.press_pos, pos).normalized().intersected(self.video.rect())

    def _apply_zoom(self, rect):
        if self.player is None:
            return
        try:
            vw = int(self.player.width)
            vh = int(self.player.height)
        except Exception:
            return
        if not vw or not vh:
            return
        w = self.video.width()
        h = self.video.height()
        scale = min(w / vw, h / vh)
        ox = (w - vw * scale) / 2
        oy = (h - vh * scale) / 2
        x0 = max(0.0, (rect.left() - ox) / scale)
        y0 = max(0.0, (rect.top() - oy) / scale)
        x1 = min(float(vw), (rect.right() - ox) / scale)
        y1 = min(float(vh), (rect.bottom() - oy) / scale)
        cw = int(x1 - x0)
        ch = int(y1 - y0)
        if cw < 16 or ch < 16:
            return
        try:
            self.player["video-crop"] = f"{cw}x{ch}+{int(x0)}+{int(y0)}"
        except Exception:
            return
        self.zoomed = True

    def shutdown(self):
        self.band.hide()
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
            t = ev.type()
            if t == QEvent.MouseButtonPress and ev.button() == Qt.LeftButton:
                if not self.focused:
                    self.on_click(self)
                else:
                    self.press_pos = ev.position().toPoint()
                    self.dragging = False
                return True
            if t == QEvent.MouseMove and self.press_pos is not None and not self.zoomed:
                pos = ev.position().toPoint()
                if not self.dragging and (pos - self.press_pos).manhattanLength() >= 8:
                    self.dragging = True
                if self.dragging:
                    rect = self._selection(pos)
                    self.band.setGeometry(
                        QRect(self.video.mapToGlobal(rect.topLeft()), rect.size())
                    )
                    self.band.show()
                return True
            if (
                t == QEvent.MouseButtonRelease
                and ev.button() == Qt.LeftButton
                and self.press_pos is not None
            ):
                dragging = self.dragging
                rect = self._selection(ev.position().toPoint()) if dragging else None
                self._cancel_drag()
                if self.zoomed:
                    self.reset_zoom()
                elif dragging:
                    self._apply_zoom(rect)
                else:
                    self.on_click(self)
                return True
            if t == QEvent.Wheel:
                dy = ev.angleDelta().y()
                if dy:
                    self.set_volume(
                        self.volume
                        + (self.volume_step if dy > 0 else -self.volume_step)
                    )
                return True
        return super().eventFilter(obj, ev)
