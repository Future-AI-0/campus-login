"""校园网界面自动登录；仅读取三个 CAMPUS_* 环境变量。"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from playwright.sync_api import (
    Error as BrowserError,
    Frame,
    Locator,
    Page,
    Response,
    sync_playwright,
)

PORTAL_HOST = "10.254.241.66"
PORTAL_URL = f"http://{PORTAL_HOST}/portal/entry/pc/authenticate;flowParams=undefined;from="
CONSENT = re.compile(r"同意|已阅读|accept|agree", re.I)
LOGIN_BUTTON = re.compile(r"^(立即登录|登录|连接|连接网络|Login|Sign in)$", re.I)
CONFIRM_BUTTON = re.compile(r"^(确定|确认|连接|OK|Confirm)$", re.I)
ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


class LoginError(RuntimeError):
    """可向用户显示的登录错误。"""


class LoginCancelled(LoginError):
    """用户取消认证。"""


def validate_url(url: str) -> None:
    target = urlsplit(url)
    if target.hostname != PORTAL_HOST or target.scheme not in ("http", "https") or not target.path.startswith("/portal/"):
        raise LoginError("入口必须是 10.254.241.66 的 /portal/ 页面。")


def redact(message: str, credentials: Credentials) -> str:
    for value in (credentials.password, credentials.username):
        if value:
            message = message.replace(value, "[已隐藏]")
    return message


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str = field(repr=False)
    operator: str

    @classmethod
    def from_environment(cls) -> Credentials:
        # 当前进程环境变量优先于脚本目录中的 .env。
        load_dotenv(ROOT / ".env", override=False, interpolate=False)
        keys = ("CAMPUS_USERNAME", "CAMPUS_PASSWORD", "CAMPUS_OPERATOR")
        values = [os.environ.get(key, "") for key in keys]
        missing = [key for key, value in zip(keys, values) if not value.strip()]
        if missing:
            raise LoginError("请设置环境变量：" + "、".join(missing))
        return cls(values[0].strip(), values[1], values[2].strip())


def unique_visible(locator: Locator, description: str) -> Locator | None:
    matches = [item for item in locator.all() if item.is_visible()]
    if len(matches) > 1:
        raise LoginError(f"{description}匹配到多个可见控件，停止操作以避免误填。")
    return matches[0] if matches else None


def trusted_frames(page: Page) -> list[Frame]:
    # 只允许在校园网认证主机的页面和 iframe 中输入账号密码。
    return [frame for frame in page.frames if urlsplit(frame.url).hostname == PORTAL_HOST]


def find_login_form(page: Page) -> Frame | None:
    matches = []
    for frame in trusted_frames(page):
        try:
            username = frame.locator('input#nameInput, input[name="username"]').filter(visible=True)
            if username.count() and frame.locator('input[type="password"]:visible').count():
                matches.append(frame)
        except BrowserError:
            if not frame.is_detached():
                raise
    if len(matches) > 1:
        raise LoginError("检测到多个登录表单，停止操作以避免误填。")
    return matches[0] if matches else None


def accept_agreement(frame: Frame) -> bool:
    checkbox = unique_visible(frame.get_by_role("checkbox", name=CONSENT), "同意框")
    if checkbox is None:
        checkbox = unique_visible(
            frame.locator('label.protocol__track input[type="checkbox"]'), "同意框"
        )
    if checkbox is None:
        # 某些学校未启用协议；有协议文字但找不到控件时不能直接提交。
        if frame.get_by_text(CONSENT).count():
            raise LoginError("页面要求同意协议，但未找到唯一的同意框。")
        return False
    # set_checked 不会把已勾选的协议再次取消。
    checkbox.set_checked(True)
    if not checkbox.is_checked():
        raise LoginError("同意框未能勾选。")
    return True


def prepare_form(frame: Frame, credentials: Credentials) -> Locator:
    username = unique_visible(
        frame.locator('input#nameInput, input[name="username"]'), "用户名输入框"
    )
    password = unique_visible(
        frame.get_by_placeholder("请输入密码", exact=True), "密码输入框"
    )
    if password is None:
        password = unique_visible(frame.locator('input[type="password"]'), "密码输入框")
    if username is None or password is None:
        raise LoginError("未找到唯一的用户名和密码输入框。")
    username.fill(credentials.username)
    password.fill(credentials.password)
    accept_agreement(frame)
    button = unique_visible(frame.locator("button#submitBtn"), "登录按钮")
    if button is None:
        button = unique_visible(frame.get_by_role("button", name=LOGIN_BUTTON), "登录按钮")
    if button is None:
        raise LoginError("未找到唯一的登录按钮。")
    return button


def operator_names(operator: str) -> list[str]:
    aliases = {
        "移动": ["移动", "中国移动"],
        "联通": ["联通", "中国联通"],
        "电信": ["电信", "中国电信"],
    }
    for names in aliases.values():
        if operator in names:
            return names
    return [operator]


def choose_operator(page: Page, operator: str) -> Locator | None:
    """实际门户用 service-box 卡片，而不是原生下拉框。"""
    expected = re.compile(r"^\s*(?:" + "|".join(map(re.escape, operator_names(operator))) + r")\s*$")
    for frame in trusted_frames(page):
        cards = frame.locator(
            "app-service-selection .service-box:visible, "
            "app-service-select-modal .service-box:visible"
        )
        if not cards.count():
            continue
        card = unique_visible(cards.filter(has_text=expected), "运营商")
        if card is None:
            available = "、".join(cards.all_inner_texts())
            raise LoginError(f"CAMPUS_OPERATOR 与页面不匹配。当前选项：{available}")
        card.click()
        panel = card.locator("xpath=ancestor::*[self::app-service-selection or self::app-service-select-modal][1]")
        button = unique_visible(panel.get_by_role("button", name=CONFIRM_BUTTON), "运营商确认按钮")
        if button is None:
            raise LoginError("找到了运营商，但没有找到唯一的确认按钮。")
        return button
    return None


@dataclass
class PortalStatus:
    online: bool = False
    error: str = ""
    terminal_failures: int = 0

    def observe(self, response: Response) -> None:
        url = urlsplit(response.url)
        if url.hostname != PORTAL_HOST:
            return
        if url.path == "/eportal/adaptor/queryTerminalInfo":
            if response.status == 555:
                self.terminal_failures += 1
            elif response.ok:
                self.terminal_failures = 0
            return
        if url.path not in (
            "/eportal/adaptor/getOnlineUserInfo",
            "/eportal/network/serviceLogin",
            "/eportal/network/operatorLogin",
        ):
            return
        try:
            payload = response.json()
        except (BrowserError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            return
        if url.path.endswith("getOnlineUserInfo"):
            info = data.get("portalOnlineUserInfo") or {}
            if isinstance(info, dict) and info.get("result") == "success":
                self.online = True
        elif not response.ok or payload.get("code") != 200:
            self.error = "运营商认证接口返回错误。请检查账号、密码和运营商。"
        elif data.get("authResult") in ("fail", "failed", "failure"):
            self.error = "运营商认证失败。请检查账号、密码和运营商。"
        # HTTP 200 和第一步账号验证成功均不等同于校园网连接成功。


def is_connected(page: Page, status: PortalStatus) -> bool:
    if status.online:
        return True
    url = urlsplit(page.url)
    return url.hostname == PORTAL_HOST and bool(
        re.fullmatch(r"/portal/entry/pc/loginSuccess(?:;.*)?/?", url.path)
    )


def check_failure(page: Page, status: PortalStatus) -> None:
    if status.error:
        raise LoginError(status.error)
    if status.terminal_failures >= 3:
        raise LoginError(
            "校园网终端信息接口持续返回 HTTP 555，无法加载认证表单。"
            "请确认网线或 Wi-Fi 已连接校园网；双网络连接、代理/TUN 或失效入口可能影响认证。"
            "可用 --url 指定校园网重新跳转生成的完整入口。"
        )
    if page.url.startswith("chrome-error:"):
        raise LoginError("认证页面或其跳转地址无法访问，请检查校园网连接和入口地址。")
    for frame in trusted_frames(page):
        error = unique_visible(
            frame.get_by_text(re.compile(r"设备未注册|账号或密码错误|用户名或密码错误|密码不正确|账户被锁定")),
            "错误提示",
        )
        if error is not None:
            raise LoginError("门户提示：" + error.inner_text()[:160])
        captcha = frame.locator('input[name="captcha_code"]:visible, input[placeholder*="验证码"]:visible')
        if captcha.count():
            raise LoginError("门户要求验证码，当前自动登录无法继续；请先在浏览器完成验证。")


def automate(
    page: Page,
    credentials: Credentials,
    timeout: float,
    dry_run: bool,
    url: str | None = None,
    progress: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> str:
    report = progress or (lambda message: print(message, flush=True))

    def check_cancelled() -> None:
        if cancelled is not None and cancelled():
            raise LoginCancelled("已取消登录。")

    check_cancelled()
    status = PortalStatus()
    page.on("response", status.observe)
    page.set_default_timeout(min(timeout * 1000, 15000))
    if url is not None:
        # 监听先于导航；commit 避免等待门户持续轮询造成的 networkidle。
        page.goto(url, wait_until="commit", timeout=min(timeout * 1000, 30000))
    deadline = time.monotonic() + timeout
    submitted = False
    operator_submitted = False
    while time.monotonic() < deadline:
        check_cancelled()
        if is_connected(page, status):
            return "校园网已连接。" if submitted or operator_submitted else "校园网当前已在线，无需重复登录。"
        check_failure(page, status)
        if not operator_submitted:
            confirm = choose_operator(page, credentials.operator)
            if confirm is not None:
                if dry_run:
                    return "演练完成：已选择运营商，没有点击确认。"
                check_cancelled()
                report("已选择运营商，正在确认连接…")
                confirm.click()
                operator_submitted = True
        if not submitted and not operator_submitted:
            frame = find_login_form(page)
            if frame is not None:
                button = prepare_form(frame, credentials)
                if dry_run:
                    return "演练完成：账号密码已填入，同意框已处理；没有提交，后续运营商步骤尚未验证。"
                check_cancelled()
                report("已填入账号密码并处理协议，正在提交登录…")
                button.click()
                submitted = True
        page.wait_for_timeout(250)
    raise LoginError("等待校园网认证结果超时；未确认连接成功。请检查入口、账号密码和运营商。")


def run_login(
    credentials: Credentials,
    *,
    url: str = PORTAL_URL,
    timeout: float = 45,
    headless: bool = False,
    dry_run: bool = False,
    progress: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> str:
    """每次在调用线程创建独立的 Playwright 和浏览器，CLI/Qt 共用。"""
    validate_url(url)
    if any(not value.strip() for value in (credentials.username, credentials.password, credentials.operator)):
        raise LoginError("请填写账号、密码和运营商。")
    report = progress or (lambda message: print(message, flush=True))
    if cancelled is not None and cancelled():
        raise LoginCancelled("已取消登录。")
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="msedge", headless=headless, args=["--no-proxy-server"])
        except BrowserError:
            browser = p.chromium.launch(headless=headless, args=["--no-proxy-server"])
        try:
            page = browser.new_page(viewport={"width": 1366, "height": 900}, locale="zh-CN")
            report("正在打开校园网认证页面…")
            return automate(page, credentials, timeout, dry_run, url, report, cancelled)
        finally:
            browser.close()


def positive_seconds(value: str) -> float:
    number = float(value)
    if not 1 <= number <= 300:
        raise argparse.ArgumentTypeError("超时时间须为 1～300 秒")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description="Playwright 校园网自动登录")
    parser.add_argument("--headless", action="store_true", help="隐藏浏览器窗口")
    parser.add_argument("--dry-run", action="store_true", help="填写界面但不提交认证")
    parser.add_argument("--timeout", type=positive_seconds, default=45, help="认证等待秒数，默认 45")
    parser.add_argument("--url", default=PORTAL_URL, help="覆盖失效入口，使用校园网重新生成的完整 URL")
    args = parser.parse_args()
    try:
        validate_url(args.url)
    except LoginError as exc:
        parser.error(str(exc))
    credentials = None
    try:
        credentials = Credentials.from_environment()
        result = run_login(credentials, url=args.url, timeout=args.timeout, headless=args.headless, dry_run=args.dry_run)
        print(result, flush=True)
        return 0
    except LoginError as exc:
        message = str(exc)
        if credentials is not None:
            message = redact(message, credentials)
        print("登录失败：" + message, file=sys.stderr)
        return 1
    except BrowserError:
        # Playwright 的原始 fill() 调用日志可能包含密码，因此不直接输出异常。
        print("浏览器操作失败。请检查校园网入口和页面，或运行 .venv\\Scripts\\python -m playwright install chromium 安装备用浏览器。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已取消登录。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
