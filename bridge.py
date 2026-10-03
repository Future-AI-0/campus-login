"""浏览器专用的临时回环转发：只访问原门户，出站绑定所选校园网卡。"""
from __future__ import annotations

import http.client
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from login import PORTAL_HOST
from network import CampusAdapter, bind_campus

HOP_HEADERS = {"connection", "proxy-connection", "proxy-authorization", "proxy-authenticate",
               "keep-alive", "te", "trailer", "transfer-encoding", "upgrade"}


def portal_target(value: str, *, tunnel: bool = False) -> tuple[int, str]:
    target = urlsplit("//" + value if tunnel else value)
    if (target.hostname != PORTAL_HOST or target.username or target.password
            or (not tunnel and target.scheme != "http")):
        raise ValueError("Only the campus portal is allowed")
    port = target.port or (443 if tunnel else 80)
    if port not in (80, 443):
        raise ValueError("Unsupported portal port")
    path = target.path or "/"
    if target.query:
        path += "?" + target.query
    return port, path


class PortalServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def handle_error(self, request, client_address):
        # Exceptions must never print request URLs, bodies, or authentication data.
        pass


class PortalHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def upstream(self, port: int):
        stream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.server.bridge.track(stream)
            bind_campus(stream, self.server.bridge.adapter)
            stream.settimeout(10)
            stream.connect((PORTAL_HOST, port))
            return stream
        except BaseException:
            stream.close()
            self.server.bridge.untrack(stream)
            raise

    def failure(self, code: int):
        self.close_connection = True
        try:
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
        except OSError:
            pass

    def handle(self):
        self.connection.settimeout(10)
        self.server.bridge.track(self.connection)
        try:
            super().handle()
        finally:
            self.server.bridge.untrack(self.connection)

    def do_CONNECT(self):
        try:
            port, _ = portal_target(self.path, tunnel=True)
        except ValueError:
            self.failure(403)
            return
        upstream = None
        try:
            upstream = self.upstream(port)
            self.send_response(200, "Connection established")
            self.end_headers()
            streams = (self.connection, upstream)
            idle_deadline = time.monotonic() + 20
            while not self.server.bridge.stopped.is_set() and time.monotonic() < idle_deadline:
                readable, _, _ = select.select(streams, [], [], .25)
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    destination = upstream if source is self.connection else self.connection
                    destination.sendall(data)
                    idle_deadline = time.monotonic() + 20
        except OSError:
            pass
        finally:
            self.close_connection = True
            if upstream is not None:
                upstream.close()
                self.server.bridge.untrack(upstream)

    def forward(self):
        try:
            port, path = portal_target(self.path)
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 <= size <= 1_048_576 or self.headers.get("Transfer-Encoding"):
                raise ValueError("Unsupported request body")
        except ValueError:
            self.failure(403)
            return
        upstream = None
        try:
            body = self.rfile.read(size)
            if len(body) != size:
                self.failure(400)
                return
            upstream = self.upstream(port)
            hop = HOP_HEADERS | {item.strip().lower() for item in self.headers.get("Connection", "").split(",")}
            headers = [f"{self.command} {path} HTTP/1.1", f"Host: {PORTAL_HOST}"]
            headers += [f"{key}: {value}" for key, value in self.headers.items()
                        if key.lower() not in hop | {"host", "content-length"}]
            headers += [f"Content-Length: {size}", "Connection: close", "", ""]
            upstream.sendall("\r\n".join(headers).encode("latin-1") + body)
            with http.client.HTTPResponse(upstream, method=self.command) as response:
                response.begin()
                self.send_response(response.status)
                response_hop = HOP_HEADERS | {item.strip().lower() for item in response.getheader("Connection", "").split(",")}
                for key, value in response.getheaders():
                    if key.lower() not in response_hop:
                        self.send_header(key, value)
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    while not self.server.bridge.stopped.is_set():
                        data = response.read1(65536)
                        if not data:
                            break
                        self.wfile.write(data)
        except (OSError, ValueError, http.client.HTTPException):
            self.failure(502)
        finally:
            self.close_connection = True
            if upstream is not None:
                upstream.close()
                self.server.bridge.untrack(upstream)

    do_GET = forward
    do_POST = forward
    do_HEAD = forward
    do_OPTIONS = forward


class CampusProxy:
    def __init__(self, adapter: CampusAdapter):
        self.adapter = adapter
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.streams = set()
        self.server = PortalServer(("127.0.0.1", 0), PortalHandler)
        self.server.bridge = self
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .1}, daemon=True)

    def track(self, stream):
        with self.lock:
            if self.stopped.is_set():
                stream.close()
                raise OSError("Portal forwarding is stopped")
            self.streams.add(stream)

    def untrack(self, stream):
        with self.lock:
            self.streams.discard(stream)

    def __enter__(self):
        self.thread.start()
        return {"server": f"http://127.0.0.1:{self.server.server_port}"}

    def __exit__(self, *args):
        self.stopped.set()
        with self.lock:
            for stream in self.streams:
                try:
                    stream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                stream.close()
            self.streams.clear()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
