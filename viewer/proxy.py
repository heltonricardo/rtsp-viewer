import re
import socket
import threading

TRANSPORT_RE = re.compile(rb"(?im)^(Transport:[ \t]*RTP/AVP)(;[^\r\n]*interleaved=)")
LENGTH_RE = re.compile(rb"(?i)content-length:[ \t]*(\d+)")


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
