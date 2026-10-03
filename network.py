"""从门户的重新跳转地址获取新入口；不发送凭据、不改变系统路由。"""
from __future__ import annotations

import ctypes
import html
import http.client
import ipaddress
import re
import secrets
import socket
import ssl
import struct
import sys
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from login import PORTAL_HOST, LoginError, validate_url

PORTAL_GATEWAYS = ("123.123.123.123", "2.2.2.2")
INTERNET_SITES = ("www.baidu.com", "www.qq.com")


@dataclass(frozen=True)
class CampusAdapter:
    index: int
    address: str
    dns_servers: tuple[str, ...]


class SocketAddress(ctypes.Structure):
    _fields_ = [("pointer", ctypes.c_void_p), ("length", ctypes.c_int)]


class AddressNode(ctypes.Structure):
    pass


AddressNode._fields_ = [
    ("alignment", ctypes.c_uint64), ("next", ctypes.POINTER(AddressNode)),
    ("address", SocketAddress),
]


class AdapterHeader(ctypes.Structure):
    pass


# Only the documented common prefix is read; Windows owns the full buffer.
AdapterHeader._fields_ = [
    ("length", ctypes.c_uint32), ("index", ctypes.c_uint32),
    ("next", ctypes.POINTER(AdapterHeader)), ("name", ctypes.c_char_p),
    ("unicast", ctypes.POINTER(AddressNode)), ("anycast", ctypes.c_void_p),
    ("multicast", ctypes.c_void_p), ("dns", ctypes.POINTER(AddressNode)),
    ("suffix", ctypes.c_wchar_p), ("description", ctypes.c_wchar_p),
    ("friendly_name", ctypes.c_wchar_p), ("physical_address", ctypes.c_ubyte * 8),
    ("physical_length", ctypes.c_uint32), ("flags", ctypes.c_uint32),
    ("mtu", ctypes.c_uint32), ("if_type", ctypes.c_uint32), ("oper_status", ctypes.c_uint32),
]


def ipv4_addresses(node) -> tuple[str, ...]:
    result = []
    while node:
        address = node.contents.address
        if address.pointer and address.length >= 8:
            raw = ctypes.string_at(address.pointer, 8)
            if struct.unpack("=H", raw[:2])[0] == socket.AF_INET:
                result.append(socket.inet_ntoa(raw[4:8]))
        node = node.contents.next
    return tuple(result)


def campus_adapter() -> CampusAdapter | None:
    """读取通往门户的活动物理网卡和它的 DNS；不能退回默认上网网卡。"""
    if sys.platform != "win32":
        return None
    index = portal_interface()
    function = ctypes.WinDLL("iphlpapi").GetAdaptersAddresses
    function.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                         ctypes.POINTER(AdapterHeader), ctypes.POINTER(ctypes.c_uint32)]
    function.restype = ctypes.c_uint32
    size = ctypes.c_uint32(15000)
    for _ in range(3):
        if size.value > 1_000_000:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        first = ctypes.cast(buffer, ctypes.POINTER(AdapterHeader))
        code = function(socket.AF_INET, 2 | 4, None, first, ctypes.byref(size))
        if code == 111:  # ERROR_BUFFER_OVERFLOW; network configuration may have changed.
            continue
        if code != 0:
            return None
        node = first
        while node:
            adapter = node.contents
            if (adapter.index == index and adapter.oper_status == 1
                    and adapter.if_type in (6, 71) and adapter.physical_length > 0):
                addresses = ipv4_addresses(adapter.unicast)
                for address in addresses:
                    parsed = ipaddress.IPv4Address(address)
                    if not (parsed.is_loopback or parsed.is_link_local or parsed.is_unspecified
                            or parsed in ipaddress.IPv4Network("198.18.0.0/15")):
                        return CampusAdapter(index, address, ipv4_addresses(adapter.dns))
            node = adapter.next
        return None
    return None


def bind_campus(stream, adapter: CampusAdapter) -> None:
    stream.setsockopt(socket.IPPROTO_IP, 31, struct.pack("!I", adapter.index))
    stream.bind((adapter.address, 0))


def remaining_timeout(deadline: float, maximum: float = 1.5) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError()
    return min(maximum, remaining)


def dns_addresses(packet: bytes, query_id: int, hostname: str) -> tuple[str, ...]:
    """有限长度解析 DNS A 回答；错误、截断和 TUN 假地址不算联网证据。"""
    def skip_name(offset: int) -> int:
        for _ in range(128):
            length = packet[offset]
            if length & 0xc0 == 0xc0:
                if offset + 1 >= len(packet):
                    raise ValueError("Truncated DNS pointer")
                return offset + 2
            if length > 63:
                raise ValueError("Invalid DNS label")
            offset += 1
            if not length:
                return offset
            offset += length
        raise ValueError("Invalid DNS name")

    ident, flags, questions, answers, _, _ = struct.unpack("!HHHHHH", packet[:12])
    question = b"".join(bytes([len(part)]) + part.encode("ascii") for part in hostname.split(".")) + b"\0"
    if (ident != query_id or not flags & 0x8000 or flags & 0x020f or questions != 1
            or packet[12:12 + len(question) + 4] != question + struct.pack("!HH", 1, 1)):
        return ()
    offset = 12 + len(question) + 4
    result = []
    for _ in range(answers):
        offset = skip_name(offset)
        kind, cls, _, length = struct.unpack("!HHIH", packet[offset:offset + 10])
        offset += 10
        if offset + length > len(packet):
            raise ValueError("Truncated DNS answer")
        if kind == 1 and cls == 1 and length == 4:
            address = socket.inet_ntoa(packet[offset:offset + 4])
            if ipaddress.IPv4Address(address).is_global:
                result.append(address)
        offset += length
    return tuple(dict.fromkeys(result))


def resolve_campus_dns(adapter: CampusAdapter, hostname: str, deadline: float) -> tuple[str, ...]:
    query_id = secrets.randbits(16)
    name = b"".join(bytes([len(part)]) + part.encode("ascii") for part in hostname.split(".")) + b"\0"
    query = struct.pack("!HHHHHH", query_id, 0x100, 1, 0, 0, 0) + name + struct.pack("!HH", 1, 1)
    for server in adapter.dns_servers:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as stream:
                bind_campus(stream, adapter)
                stream.settimeout(remaining_timeout(deadline, 0.8))
                stream.connect((server, 53))
                stream.send(query)
                addresses = dns_addresses(stream.recv(4096), query_id, hostname)
                if addresses:
                    return addresses
        except (OSError, ValueError, IndexError, struct.error):
            continue
    return ()


def verify_campus_https(adapter: CampusAdapter, hostname: str, address: str, deadline: float) -> bool:
    context = ssl.create_default_context()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        bind_campus(stream, adapter)
        stream.settimeout(remaining_timeout(deadline))
        stream.connect((address, 443))
        stream.settimeout(remaining_timeout(deadline))
        with context.wrap_socket(stream, server_hostname=hostname) as secure:
            secure.settimeout(remaining_timeout(deadline))
            secure.sendall((f"HEAD / HTTP/1.1\r\nHost: {hostname}\r\nConnection: close\r\n\r\n").encode("ascii"))
            with http.client.HTTPResponse(secure) as response:
                response.begin()
                # No redirects are followed, especially redirects to a login page.
                return response.status == 200


def internet_available(timeout: float = 6) -> bool | None:
    """True 是同一校园网网卡的 TLS+HTTP 证据；超时/失败均为未知，不代表未登录。"""
    deadline = time.monotonic() + timeout
    try:
        adapter = campus_adapter()
        if adapter is None:
            return None
        # The selected physical adapter must also reach the campus portal.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
            bind_campus(stream, adapter)
            stream.settimeout(remaining_timeout(deadline))
            stream.connect((PORTAL_HOST, 80))
        for hostname in INTERNET_SITES:
            for address in resolve_campus_dns(adapter, hostname, deadline)[:2]:
                try:
                    if verify_campus_https(adapter, hostname, address, deadline):
                        return True
                except (OSError, ValueError, http.client.HTTPException):
                    pass
    except (OSError, ValueError, http.client.HTTPException):
        pass
    return None


def extract_entry(base_url: str, location: str = "", content: str = "") -> str | None:
    """网关可能返回 HTTP 跳转，也可能返回包含 JS 跳转的短 HTML。"""
    decoded = html.unescape(content).replace("\\/", "/")
    candidates = [urljoin(base_url, location)] if location else []
    candidates += re.findall(r"https?://[^\s<>\"']+", decoded)
    for candidate in candidates:
        try:
            validate_url(candidate)
            return candidate
        except LoginError:
            pass
    return None


def portal_interface() -> int | None:
    if sys.platform != "win32":
        return None
    from ctypes import wintypes
    library = ctypes.WinDLL("iphlpapi")
    function = library.GetBestInterface
    function.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    function.restype = wintypes.DWORD
    index = wintypes.DWORD()
    address = int.from_bytes(socket.inet_aton(PORTAL_HOST), "little")
    if function(address, ctypes.byref(index)) != 0:
        raise OSError("Cannot find campus interface")
    return index.value


def resolve_entry(redirect_url: str, remaining: float) -> str | None:
    """最多进行一次有限时长的请求，只接受原校园门户的新登录入口。"""
    if remaining <= 0 or any(char in redirect_url for char in "\r\n"):
        return None
    try:
        validate_url(redirect_url)
        return redirect_url
    except LoginError:
        pass
    try:
        target = urlsplit(redirect_url)
        if (target.scheme != "http" or target.hostname not in PORTAL_GATEWAYS
                or target.username or target.password or target.port not in (None, 80)
                ):
            return None
        index = portal_interface()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
            stream.settimeout(min(3.0, remaining))
            if index is not None:
                # Windows IP_UNICAST_IF 只控制这个 socket 的发送接口。
                stream.setsockopt(socket.IPPROTO_IP, 31, struct.pack("!I", index))
            stream.connect((target.hostname, 80))
            path = target.path or "/"
            if target.query:
                path += "?" + target.query
            request = (f"GET {path} HTTP/1.1\r\nHost: {target.hostname}\r\n"
                       "User-Agent: CampusLogin/1.0\r\nConnection: close\r\n\r\n")
            stream.sendall(request.encode("ascii"))
            with http.client.HTTPResponse(stream) as response:
                response.begin()
                if response.status not in (200, 301, 302, 303, 307, 308):
                    return None
                fresh_url = extract_entry(redirect_url, response.getheader("Location", ""))
                if fresh_url:
                    return fresh_url
                return extract_entry(redirect_url, content=response.read(65536).decode("utf-8", errors="replace"))
    except (OSError, ValueError, UnicodeError, http.client.HTTPException, LoginError):
        return None
