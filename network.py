"""从门户的重新跳转地址获取新入口；不发送凭据、不改变系统路由。"""
from __future__ import annotations

import ctypes
import html
import http.client
import re
import socket
import struct
import sys
from urllib.parse import urljoin, urlsplit

from login import PORTAL_HOST, LoginError, validate_url

PORTAL_GATEWAYS = ("123.123.123.123", "2.2.2.2")


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
