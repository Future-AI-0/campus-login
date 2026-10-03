"""仅使用本地服务器验证浏览器转发；不访问校园网，不提交真实账号。"""
import http.client
import socket
import socketserver
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

from bridge import CampusProxy, PortalHandler, portal_target
from network import CampusAdapter


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        self.server.received.append((self.path, body, self.headers.get('Proxy-Authorization')))
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Set-Cookie', 'fixture=1; Path=/')
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        while data := self.request.recv(65536):
            self.request.sendall(data)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.adapter = CampusAdapter(11, '10.0.0.2', (), 'wifi')
        self.fixture = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
        self.fixture.received = []
        self.fixture_thread = threading.Thread(target=self.fixture.serve_forever, daemon=True)
        self.fixture_thread.start()

    def tearDown(self):
        self.fixture.shutdown()
        self.fixture.server_close()
        self.fixture_thread.join(2)

    def test_post_body_query_cookies_and_proxy_headers(self):
        fixture = self.fixture
        adapter = self.adapter
        def open_fixture(handler, port):
            self.assertEqual(handler.server.bridge.adapter, adapter)
            return socket.create_connection(fixture.server_address, timeout=2)
        with patch.object(PortalHandler, 'upstream', open_fixture), CampusProxy(self.adapter) as proxy:
            connection = http.client.HTTPConnection(urlsplit(proxy['server']).netloc, timeout=2)
            connection.request('POST', 'http://10.254.241.66/ssoAuthen/login?fixture=1', b'test-user|test-password',
                               {'Proxy-Authorization': 'fixture-secret', 'Content-Type': 'text/plain'})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader('Set-Cookie'), 'fixture=1; Path=/')
            self.assertEqual(response.read(), b'test-user|test-password')
            connection.close()
        self.assertEqual(self.fixture.received, [('/ssoAuthen/login?fixture=1', b'test-user|test-password', None)])

    def test_untrusted_host_and_oversized_body_never_open_upstream(self):
        with patch.object(PortalHandler, 'upstream') as open_upstream, CampusProxy(self.adapter) as proxy:
            for method, target, headers in (
                ('GET', 'http://example.test/portal/login', {}),
                ('CONNECT', 'example.test:443', {}),
                ('POST', 'http://10.254.241.66/login', {'Content-Length': '1048577'}),
            ):
                connection = http.client.HTTPConnection(urlsplit(proxy['server']).netloc, timeout=2)
                connection.request(method, target, headers=headers)
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
                connection.close()
            open_upstream.assert_not_called()

    def test_https_connect_forwards_opaque_bytes(self):
        fixture = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Echo)
        worker = threading.Thread(target=fixture.serve_forever, daemon=True)
        worker.start()
        try:
            with patch.object(PortalHandler, 'upstream', lambda handler, port: socket.create_connection(fixture.server_address, timeout=2)), CampusProxy(self.adapter) as proxy:
                target = urlsplit(proxy['server'])
                with socket.create_connection((target.hostname, target.port), timeout=2) as client:
                    client.sendall(b'CONNECT 10.254.241.66:443 HTTP/1.1\r\nHost: 10.254.241.66:443\r\n\r\n')
                    headers = b''
                    while not headers.endswith(b'\r\n\r\n'):
                        headers += client.recv(1)
                    self.assertIn(b'200 Connection established', headers)
                    data = b'\x16\x03\x01opaque-encrypted-payload'
                    client.sendall(data)
                    self.assertEqual(client.recv(65536), data)
        finally:
            fixture.shutdown()
            fixture.server_close()
            worker.join(2)

    def test_upstream_binds_selected_wifi_without_default_route_retry(self):
        handler = object.__new__(PortalHandler)
        handler.server = MagicMock()
        handler.server.bridge.adapter = self.adapter
        stream = MagicMock()
        with patch('bridge.socket.socket', return_value=stream), patch('bridge.bind_campus') as bind:
            self.assertIs(handler.upstream(80), stream)
            bind.assert_called_once_with(stream, self.adapter)
            stream.connect.assert_called_once_with(('10.254.241.66', 80))
        with patch('bridge.socket.socket', return_value=stream), patch('bridge.bind_campus', side_effect=OSError()):
            with self.assertRaises(OSError):
                handler.upstream(80)
            stream.close.assert_called()

    def test_shutdown_cancels_stalled_client_and_removes_local_listener(self):
        bridge = CampusProxy(self.adapter)
        with bridge as proxy:
            endpoint = urlsplit(proxy['server'])
            client = socket.create_connection((endpoint.hostname, endpoint.port), timeout=2)
            client.sendall(b'POST http://10.254.241.66/login HTTP/1.1\r\nContent-Length: 10\r\n\r\n')
            deadline = time.monotonic() + 2
            while not bridge.streams and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(bridge.streams)
        self.assertFalse(bridge.thread.is_alive())
        self.assertEqual(bridge.streams, set())
        client.close()
        with socket.socket() as probe:
            self.assertNotEqual(probe.connect_ex((endpoint.hostname, endpoint.port)), 0)

    def test_target_rejects_userinfo_and_non_portal_ports(self):
        for target in ('http://user:password@10.254.241.66/login', 'http://10.254.241.66:1234/login', 'https://10.254.241.66/login'):
            with self.assertRaises(ValueError):
                portal_target(target)


if __name__ == '__main__':
    unittest.main()
