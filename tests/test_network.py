"""入口刷新只读取受限网关，绝不向网关发送账号。"""
import socket
import struct
import unittest
from unittest.mock import MagicMock, patch

from login import PORTAL_URL
from network import extract_entry, resolve_entry


class EntryTests(unittest.TestCase):
    def test_legacy_html_redirect_retains_current_gateway_parameters(self):
        expected = 'http://10.254.241.66/eportal/index.jsp?wlanuserip=example&wlanacname=example'
        content = "<script>location.href='" + expected.replace('&', '&amp;') + "'</script>"
        self.assertEqual(extract_entry('http://123.123.123.123', content=content), expected)
        self.assertEqual(extract_entry('http://123.123.123.123', location=PORTAL_URL), PORTAL_URL)

    def test_gateway_cannot_redirect_credentials_to_other_site_or_service(self):
        self.assertIsNone(extract_entry('http://123.123.123.123', location='http://example.test/portal/login'))
        self.assertIsNone(extract_entry('http://123.123.123.123', content="<a href='http://10.254.241.66/admin/'>Help</a>"))

    def test_unknown_or_invalid_gateway_is_not_requested(self):
        with patch('network.socket.socket') as factory:
            for url in ('http://example.test/', 'https://123.123.123.123/', 'http://123.123.123.123:bad/', 'http://user:password@123.123.123.123/'):
                self.assertIsNone(resolve_entry(url, 3))
            factory.assert_not_called()

    def test_gateway_uses_campus_interface_and_accepts_html_redirect(self):
        sock = MagicMock()
        response = MagicMock(status=200)
        response.__enter__.return_value = response
        response.getheader.return_value = ''
        response.read.return_value = b"<script>location.href='http://10.254.241.66/eportal/index.jsp?example=1'</script>"
        with patch('network.portal_interface', return_value=3), patch('network.socket.socket') as factory, patch('network.http.client.HTTPResponse', return_value=response):
            factory.return_value.__enter__.return_value = sock
            self.assertEqual(resolve_entry('http://123.123.123.123/', 5), 'http://10.254.241.66/eportal/index.jsp?example=1')
        sock.setsockopt.assert_called_once_with(socket.IPPROTO_IP, 31, struct.pack('!I',3))
        sock.connect.assert_called_once_with(('123.123.123.123',80))
        request = sock.sendall.call_args.args[0]
        self.assertTrue(request.startswith(b'GET / HTTP/1.1\r\n'))
        self.assertNotIn(b'CAMPUS_PASSWORD', request)

    def test_gateway_timeout_does_not_report_success(self):
        with patch('network.portal_interface', return_value=3), patch('network.socket.socket') as factory:
            factory.return_value.__enter__.return_value.connect.side_effect = TimeoutError()
            self.assertIsNone(resolve_entry('http://123.123.123.123/', 1))


if __name__ == '__main__':
    unittest.main()
