"""入口刷新只读取受限网关，绝不向网关发送账号。"""
import socket
import struct
import time
import unittest
from unittest.mock import MagicMock, patch

from login import PORTAL_URL
from network import (CampusAdapter, bind_campus, dns_addresses, extract_entry,
                     internet_available, resolve_entry, verify_campus_https)


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


class ConnectivityTests(unittest.TestCase):
    def setUp(self):
        self.adapter = CampusAdapter(3, '10.0.0.2', ('10.0.0.1',))

    def packet(self, address='8.8.8.8', ident=42):
        question = b'\x03www\x05baidu\x03com\0' + struct.pack('!HH', 1, 1)
        return (struct.pack('!HHHHHH', ident, 0x8180, 1, 1, 0, 0) + question
                + b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 60, 4) + socket.inet_aton(address))

    def test_dns_rejects_tun_private_addresses_mismatch_and_truncation(self):
        self.assertEqual(dns_addresses(self.packet(), 42, 'www.baidu.com'), ('8.8.8.8',))
        for address in ('198.18.0.3', '198.19.1.1', '10.0.0.2', '127.0.0.1'):
            self.assertEqual(dns_addresses(self.packet(address), 42, 'www.baidu.com'), ())
        self.assertEqual(dns_addresses(self.packet(ident=43), 42, 'www.baidu.com'), ())
        self.assertEqual(dns_addresses(self.packet(), 42, 'www.qq.com'), ())
        with self.assertRaises(ValueError):
            dns_addresses(self.packet()[:-1], 42, 'www.baidu.com')

    def test_binding_fixes_both_interface_and_source_ip(self):
        stream = MagicMock()
        bind_campus(stream, self.adapter)
        stream.setsockopt.assert_called_once_with(socket.IPPROTO_IP, 31, struct.pack('!I', 3))
        stream.bind.assert_called_once_with(('10.0.0.2', 0))

    def test_cannot_identify_campus_adapter_does_not_use_system_internet(self):
        with patch('network.campus_adapter', return_value=None), patch('network.socket.socket') as factory:
            self.assertIsNone(internet_available())
            factory.assert_not_called()

    def test_unreachable_campus_portal_does_not_claim_hotspot_internet(self):
        with patch('network.campus_adapter', return_value=self.adapter), patch('network.socket.socket') as factory, patch('network.verify_campus_https') as verify:
            factory.return_value.__enter__.return_value.connect.side_effect = TimeoutError()
            self.assertIsNone(internet_available())
            verify.assert_not_called()

    def test_one_site_failure_falls_back_and_all_failures_are_unknown(self):
        with patch('network.campus_adapter', return_value=self.adapter), patch('network.socket.socket'), patch('network.resolve_campus_dns', return_value=('8.8.8.8',)), patch('network.verify_campus_https', side_effect=[TimeoutError(), True]) as verify:
            self.assertIs(internet_available(), True)
            self.assertEqual(verify.call_args.args[1], 'www.qq.com')
        with patch('network.campus_adapter', return_value=self.adapter), patch('network.socket.socket'), patch('network.resolve_campus_dns', return_value=('8.8.8.8',)), patch('network.verify_campus_https', side_effect=TimeoutError()):
            self.assertIsNone(internet_available())

    def test_https_validates_hostname_and_does_not_accept_redirect(self):
        stream = MagicMock()
        context = MagicMock()
        response = MagicMock(status=302)
        response.__enter__.return_value = response
        with patch('network.socket.socket') as factory, patch('network.ssl.create_default_context', return_value=context), patch('network.http.client.HTTPResponse', return_value=response):
            factory.return_value.__enter__.return_value = stream
            self.assertFalse(verify_campus_https(self.adapter, 'www.baidu.com', '8.8.8.8', time.monotonic() + 5))
            context.wrap_socket.assert_called_once_with(stream, server_hostname='www.baidu.com')
            response.status = 200
            self.assertTrue(verify_campus_https(self.adapter, 'www.baidu.com', '8.8.8.8', time.monotonic() + 5))


if __name__ == '__main__':
    unittest.main()
