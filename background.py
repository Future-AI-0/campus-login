"""静默检测的网络探测、重试策略与偏好配置。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from login import PORTAL_HOST, PORTAL_URL, validate_url

PROBE_INTERVAL_MS = 15_000


class PortalRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if target.hostname != PORTAL_HOST or target.scheme not in ("http", "https"):
            raise HTTPError(req.full_url, code, "Non-portal redirect", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def probe_reachable(url: str, timeout: float = 3) -> bool:
    """只检查校园网 HTTP 响应，不发账号、不使用显式代理、不探测公网。"""
    validate_url(url)
    request = Request(url, headers={"User-Agent": "CampusLogin/1.0", "Cache-Control": "no-cache"})
    opener = build_opener(ProxyHandler({}), PortalRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            return 200 <= response.status < 300 and urlsplit(response.url).hostname == PORTAL_HOST
    except (HTTPError, URLError, OSError, TimeoutError, ValueError):
        return False


@dataclass
class AutoLoginPolicy:
    next_attempt: float = 0
    failures: int = 0
    reachable: bool | None = None

    def probe(self, reachable: bool, now: float) -> bool:
        if not reachable:
            # 断网后重新连接时立即再试，不继承上一条连接的冷却时间。
            self.next_attempt = 0
            self.failures = 0
        self.reachable = reachable
        return reachable and now >= self.next_attempt

    def completed(self, success: bool, now: float) -> None:
        if success:
            self.failures = 0
            # 后续只间隔确认；登录流程遇到“已在线”不会再提交凭据。
            self.next_attempt = now + 300
        else:
            self.failures += 1
            self.next_attempt = now + min(60 * 2 ** min(self.failures - 1, 3), 300)


def load_preferences(path: Path) -> dict:
    defaults = {"auto_login": True, "entry_url": PORTAL_URL, "timeout": 45, "show_browser": True}
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(values, dict):
            return defaults
        for key in ("auto_login", "show_browser"):
            if isinstance(values.get(key), bool):
                defaults[key] = values[key]
        if isinstance(values.get("entry_url"), str):
            validate_url(values["entry_url"])
            defaults["entry_url"] = values["entry_url"]
        if isinstance(values.get("timeout"), int) and 1 <= values["timeout"] <= 300:
            defaults["timeout"] = values["timeout"]
    except (OSError, ValueError, RuntimeError):
        pass
    return defaults


def save_preferences(path: Path, values: dict) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)
