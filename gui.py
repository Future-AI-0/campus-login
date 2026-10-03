"""校园网登录的 Qt 桌面界面。"""
from __future__ import annotations

import os
import sys
import threading
import time
import tempfile
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values, set_key
from PySide6.QtCore import QSignalBlocker, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QColor, QCloseEvent, QFont, QPalette
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QPlainTextEdit,
    QProgressBar, QPushButton, QSpinBox, QStyle, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from background import AutoLoginPolicy, PROBE_INTERVAL_MS, load_preferences, probe_reachable, save_preferences
from desktop import SingleInstance, ensure_desktop_shortcut, set_startup, startup_enabled
from login import (
    BrowserError, Credentials, LoginCancelled, LoginError, PORTAL_URL,
    ROOT, redact, run_login, validate_url,
)

OPERATORS = ("中国移动", "中国联通", "中国电信", "校园网")


def canonical_operator(value: str) -> str:
    aliases = {"移动": "中国移动", "联通": "中国联通", "电信": "中国电信"}
    value = aliases.get(value.strip(), value.strip())
    if value and value not in OPERATORS:
        raise LoginError("请选择支持的运营商：中国移动、中国联通、中国电信或校园网。")
    return value


def read_config(path: Path) -> dict[str, str]:
    stored = dotenv_values(path, interpolate=False) if path.exists() else {}
    keys = ("CAMPUS_USERNAME", "CAMPUS_PASSWORD", "CAMPUS_OPERATOR")
    # 界面修改的本机配置优先，避免环境变量使自动登录继续使用旧账号。
    return {key: (stored[key] or "") if key in stored else os.environ.get(key, "") for key in keys}


def save_config(path: Path, credentials: Credentials) -> None:
    # 先写完整快照再替换，后台线程不会读到新旧账号混合的配置。
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        if path.exists():
            temporary.write_bytes(path.read_bytes())
        for key, value in zip(
            ("CAMPUS_USERNAME", "CAMPUS_PASSWORD", "CAMPUS_OPERATOR"),
            (credentials.username, credentials.password, credentials.operator),
        ):
            quoted = "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
            set_key(temporary, key, quoted, quote_mode="never", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_account_file(path: Path) -> Credentials:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("gb18030")
    parts = text.rstrip("\r\n").split("|")
    if len(parts) != 3 or any("\n" in part or "\r" in part for part in parts):
        raise LoginError("账号文件应只有一行，格式为：账号|密码|运营商。")
    credentials = Credentials(parts[0].strip(), parts[1], parts[2].strip())
    if any(not item.strip() for item in (credentials.username, credentials.password, credentials.operator)):
        raise LoginError("账号文件中的账号、密码和运营商都不能为空。")
    return credentials


def friendly_error(message: str) -> str:
    return message.replace("CAMPUS_OPERATOR", "运营商")


class LoginWorker(QThread):
    progress = Signal(str)
    result = Signal(bool, str)

    def __init__(self, credentials: Credentials, *, url: str, timeout: int, headless: bool, parent=None):
        super().__init__(parent)
        self.credentials = credentials
        self.url = url
        self.timeout = timeout
        self.headless = headless
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        # Playwright 在此线程内创建，所有浏览器操作均留在此线程。
        try:
            message = run_login(
                self.credentials, url=self.url, timeout=self.timeout,
                headless=self.headless,
                progress=lambda text: self.progress.emit(redact(text, self.credentials)),
                cancelled=self.cancel_event.is_set,
            )
            self.result.emit(True, redact(message, self.credentials))
        except LoginCancelled:
            self.result.emit(False, "已取消登录，浏览器已关闭。")
        except LoginError as exc:
            self.result.emit(False, friendly_error(redact(str(exc), self.credentials)))
        except BrowserError:
            message = "浏览器操作未完成。请确认电脑已安装 Microsoft Edge，并检查校园网入口。"
            if self.cancel_event.is_set():
                message = "已取消登录，浏览器已关闭。"
            self.result.emit(False, message)
        except Exception:
            # 原始异常可能含凭据或认证令牌，界面不输出堆栈。
            self.result.emit(False, "登录程序遇到异常。请检查网络和配置后重试。")


class ProbeWorker(QThread):
    result = Signal(bool)

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self.url = url

    def run(self) -> None:
        try:
            reachable = probe_reachable(self.url)
        except Exception:
            reachable = False
        self.result.emit(reachable)


STYLE = """
QMainWindow { background: #f3f6fb; }
QWidget { font-family: 'Microsoft YaHei UI'; font-size: 13px; color: #23334a; }
QFrame#card { background: white; border: 1px solid #e2e8f1; border-radius: 12px; }
QLabel#title { font-size: 25px; font-weight: 700; color: #14243b; }
QLabel#subtitle, QLabel#note { color: #6d7d93; }
QLineEdit, QComboBox, QSpinBox { background: #fbfcff; border: 1px solid #d9e2ef; border-radius: 6px; padding: 9px; min-height: 18px; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border: 1px solid #367df4; }
QPushButton { border: 1px solid #d9e2ef; border-radius: 6px; background: white; padding: 9px 14px; }
QPushButton:hover { background: #edf3fe; }
QPushButton#login { background: #296df1; color: white; border: none; font-weight: 600; }
QPushButton#login:hover { background: #1c5ed9; }
QPushButton:disabled { background: #e9edf4; color: #9ba8b9; }
QPushButton#advanced { color: #3973cd; border: none; padding: 4px 0; text-align: left; background: transparent; }
QCheckBox { spacing: 7px; }
QPlainTextEdit { background: #f8faff; border: 1px solid #e4eaf3; border-radius: 6px; padding: 7px; font-size: 12px; }
QProgressBar { background: #e7eefb; border: none; border-radius: 2px; max-height: 4px; }
QProgressBar::chunk { background: #367df4; }
"""

TRAY_STYLE = """
QMenu { background-color: #ffffff; color: #23334a; border: 1px solid #d9e2ef;
        padding: 5px; font-family: 'Microsoft YaHei UI'; font-size: 13px; }
QMenu::item { color: #23334a; background-color: transparent; padding: 8px 28px; }
QMenu::item:selected { color: #ffffff; background-color: #296df1; border-radius: 4px; }
QMenu::item:disabled { color: #7b8798; }
QMenu::separator { height: 1px; background-color: #e2e8f1; margin: 5px 9px; }
"""


class LoginWindow(QMainWindow):
    def __init__(self, config_path: Path | None = None, *, background_services: bool = True):
        super().__init__()
        self.config_path = config_path or ROOT / ".env"
        self.settings_path = self.config_path.with_name("settings.json")
        self.preferences = load_preferences(self.settings_path)
        self.background_services = background_services
        self.policy = AutoLoginPolicy()
        self.account_edited_at = 0.0
        self.worker: LoginWorker | None = None
        self.probe_worker: ProbeWorker | None = None
        self.automatic_attempt = False
        self.tray: QSystemTrayIcon | None = None
        self.tray_menu: QMenu | None = None
        self.quitting = False
        self.close_pending = False
        self.restore_timer = QTimer(self)
        self.restore_timer.setSingleShot(True)
        self.restore_timer.timeout.connect(self.restore_window)
        self.activation_timer = QTimer(self)
        self.activation_timer.setSingleShot(True)
        self.activation_timer.timeout.connect(self.activate_restored_window)
        self.setWindowTitle("校园网登录")
        self.setMinimumWidth(510)
        self.resize(550, 775)
        self.setStyleSheet(STYLE)

        container = QWidget()
        self.setCentralWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(28, 26, 28, 24)
        layout.setSpacing(15)
        title = QLabel("校园网登录")
        title.setObjectName("title")
        layout.addWidget(title)
        subtitle = QLabel("连接校园网后，自动完成认证")
        subtitle.setObjectName("subtitle")
        layout.addWidget(subtitle)

        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(22, 22, 22, 20)
        card_layout.setSpacing(15)
        form = QFormLayout()
        form.setSpacing(13)
        self.username = QLineEdit()
        self.username.setPlaceholderText("请输入校园网账号")
        self.username.setAccessibleName("校园网账号")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("请输入校园网密码")
        self.password.setAccessibleName("校园网密码")
        self.operator = QComboBox()
        self.operator.setEditable(False)
        self.operator.addItems(OPERATORS)
        self.operator.setPlaceholderText("请选择运营商")
        self.operator.setCurrentIndex(-1)
        self.operator.setAccessibleName("运营商")
        form.addRow("账号", self.username)
        form.addRow("密码", self.password)
        form.addRow("运营商", self.operator)
        card_layout.addLayout(form)

        options = QHBoxLayout()
        self.remember = QCheckBox("自动保存到本机")
        self.remember.setToolTip("修改账号、密码或运营商时，立即更新程序旁的 .env 文件")
        self.remember.setChecked(True)
        self.show_password = QCheckBox("显示密码")
        self.show_password.toggled.connect(lambda checked: self.password.setEchoMode(QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password))
        options.addWidget(self.remember)
        options.addStretch()
        options.addWidget(self.show_password)
        card_layout.addLayout(options)

        utilities = QHBoxLayout()
        self.import_button = QPushButton("导入账号文件")
        self.import_button.clicked.connect(self.import_account)
        self.save_button = QPushButton("保存配置")
        self.save_button.clicked.connect(self.save_account)
        utilities.addWidget(self.import_button)
        utilities.addWidget(self.save_button)
        utilities.addStretch()
        card_layout.addLayout(utilities)

        self.auto_login = QCheckBox("入口可达时静默自动登录")
        self.auto_login.setChecked(self.preferences["auto_login"])
        self.auto_login.setToolTip("每 15 秒检测校园网入口，使用已保存的配置在后台认证；失败后间隔重试")
        card_layout.addWidget(self.auto_login)

        self.startup = QCheckBox("开机启动（登录 Windows 后）")
        self.startup.setToolTip("登录 Windows 后静默进入托盘；取消勾选即可删除本程序的启动项")
        self.startup.setChecked(startup_enabled() if self.background_services else False)
        self.startup.toggled.connect(self.toggle_startup)
        card_layout.addWidget(self.startup)

        self.advanced_button = QPushButton("高级设置")
        self.advanced_button.setObjectName("advanced")
        card_layout.addWidget(self.advanced_button)
        self.advanced_panel = QWidget()
        advanced_form = QFormLayout(self.advanced_panel)
        advanced_form.setContentsMargins(0, 0, 0, 0)
        self.entry_url = QLineEdit(self.preferences["entry_url"])
        self.entry_url.setAccessibleName("校园网入口")
        self.timeout = QSpinBox()
        self.timeout.setRange(1, 300)
        self.timeout.setValue(self.preferences["timeout"])
        self.timeout.setSuffix(" 秒")
        self.show_browser = QCheckBox("手动登录时显示浏览器")
        self.show_browser.setChecked(self.preferences["show_browser"])
        advanced_form.addRow("登录入口", self.entry_url)
        advanced_form.addRow("等待时间", self.timeout)
        advanced_form.addRow("", self.show_browser)
        self.advanced_panel.hide()
        self.advanced_button.clicked.connect(self.toggle_advanced)
        card_layout.addWidget(self.advanced_panel)

        actions = QHBoxLayout()
        self.login_button = QPushButton("连接校园网")
        self.login_button.setObjectName("login")
        self.login_button.setDefault(True)
        self.login_button.clicked.connect(lambda: self.start_login())
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_login)
        actions.addWidget(self.login_button, 1)
        actions.addWidget(self.cancel_button)
        card_layout.addLayout(actions)
        layout.addWidget(card)

        self.status = QLabel("就绪")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setAccessibleName("连接状态")
        layout.addWidget(self.status)
        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setTextVisible(False)
        self.busy.hide()
        layout.addWidget(self.busy)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("连接进度会显示在这里")
        self.log.setMaximumBlockCount(100)
        self.log.setMinimumHeight(100)
        self.log.setAccessibleName("连接进度")
        layout.addWidget(self.log, 1)
        note = QLabel("自动登录不弹出浏览器；关闭窗口可继续在托盘检测。")
        note.setObjectName("note")
        note.setWordWrap(True)
        layout.addWidget(note)

        try:
            values = read_config(self.config_path)
            self.apply_credentials(Credentials(values["CAMPUS_USERNAME"], values["CAMPUS_PASSWORD"], values["CAMPUS_OPERATOR"]))
        except (OSError, UnicodeError, LoginError):
            self.show_status("无法读取本机配置，可以手动填写后登录。", False)

        self.username.textChanged.connect(self.autosave_account)
        self.password.textChanged.connect(self.autosave_account)
        self.operator.currentIndexChanged.connect(self.autosave_account)
        self.remember.toggled.connect(self.autosave_account)

        self.monitor_timer = QTimer(self)
        self.monitor_timer.setInterval(PROBE_INTERVAL_MS)
        self.monitor_timer.timeout.connect(self.check_portal)
        self.auto_login.toggled.connect(self.toggle_monitoring)
        self.entry_url.editingFinished.connect(self.persist_preferences)
        self.timeout.valueChanged.connect(self.persist_preferences)
        self.show_browser.toggled.connect(self.persist_preferences)
        if self.background_services:
            self.create_tray()
            if self.auto_login.isChecked():
                self.monitor_timer.start()
                QTimer.singleShot(500, self.check_portal)

    def create_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DriveNetIcon)
        self.setWindowIcon(icon)
        self.tray = QSystemTrayIcon(icon, self)
        self.tray_menu = self.create_tray_menu()
        self.tray.setContextMenu(self.tray_menu)
        self.tray.setToolTip("校园网登录 · 等待检测")
        self.tray.activated.connect(self.tray_activated)
        self.tray.show()
        QApplication.instance().setQuitOnLastWindowClosed(False)

    def create_tray_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.setObjectName("trayMenu")
        # 系统可能采用深色主题；菜单的文字、底色和选中色必须同时指定。
        palette = QPalette(menu.palette())
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
            palette.setColor(role, QColor("#ffffff"))
        for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
            palette.setColor(role, QColor("#23334a"))
        palette.setColor(QPalette.ColorRole.Highlight, QColor("#296df1"))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
        menu.setPalette(palette)
        menu.setStyleSheet(TRAY_STYLE)
        open_action = menu.addAction("打开窗口")
        open_action.triggered.connect(self.show_window)
        self.auto_action = menu.addAction("静默自动登录")
        self.auto_action.setCheckable(True)
        self.auto_action.setChecked(self.auto_login.isChecked())
        self.auto_action.toggled.connect(self.auto_login.setChecked)
        self.auto_login.toggled.connect(self.auto_action.setChecked)
        connect_action = menu.addAction("立即连接")
        connect_action.triggered.connect(lambda: self.start_login(automatic=True))
        menu.addSeparator()
        quit_action = menu.addAction("退出")
        quit_action.triggered.connect(self.request_exit)
        return menu

    def tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.DoubleClick, QSystemTrayIcon.ActivationReason.Trigger):
            self.show_window()

    @Slot()
    def show_window(self) -> None:
        if not self.quitting:
            # 等托盘菜单结束关闭和焦点交还后再恢复窗口。
            self.restore_timer.start(25)

    def restore_window(self) -> None:
        if self.quitting:
            return
        if self.tray_menu is not None:
            self.tray_menu.hide()
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.show()
        self.activate_restored_window()
        # Windows 菜单的原生关闭通知可能晚于 Qt 的 triggered 信号。
        self.activation_timer.start(100)

    def activate_restored_window(self) -> None:
        if self.quitting or not self.isVisible() or self.isMinimized():
            return
        self.raise_()
        self.activateWindow()
        handle = self.windowHandle()
        if handle is not None:
            handle.requestActivate()

    def persist_preferences(self) -> None:
        try:
            url = self.entry_url.text().strip()
            validate_url(url)
            values = {"auto_login": self.auto_login.isChecked(), "entry_url": url,
                      "timeout": self.timeout.value(), "show_browser": self.show_browser.isChecked()}
            save_preferences(self.settings_path, values)
            self.preferences = values
        except (OSError, LoginError):
            # 偏好写入失败不阻断当前登录，也不写账号到偏好文件。
            pass

    def saved_credentials(self) -> Credentials:
        values = read_config(self.config_path)
        result = Credentials(values["CAMPUS_USERNAME"], values["CAMPUS_PASSWORD"], canonical_operator(values["CAMPUS_OPERATOR"]))
        if any(not value.strip() for value in (result.username, result.password, result.operator)):
            raise LoginError("自动登录需要先填写并保存账号、密码和运营商。")
        return result

    def toggle_startup(self, enabled: bool) -> None:
        try:
            set_startup(enabled)
            self.append_log("已启用开机启动，登录 Windows 后将在托盘运行。" if enabled else "已关闭开机启动。")
        except OSError:
            with QSignalBlocker(self.startup):
                self.startup.setChecked(startup_enabled())
            self.show_status("无法修改开机启动项，请检查当前用户的 Windows 权限。", False)

    def autosave_account(self) -> bool:
        if not self.remember.isChecked():
            return True
        self.account_edited_at = time.monotonic()
        try:
            # 空字段也保存，清空输入后不会继续使用旧凭据。
            save_config(self.config_path, Credentials(
                self.username.text().strip(), self.password.text(), self.operator.currentText(),
            ))
            self.policy = AutoLoginPolicy()
            return True
        except OSError:
            self.show_status("无法自动保存配置，请确认程序文件夹可以写入。", False)
            return False

    def toggle_monitoring(self, enabled: bool) -> None:
        self.persist_preferences()
        if enabled:
            self.policy = AutoLoginPolicy()
            if self.background_services and not self.quitting:
                self.monitor_timer.start()
                QTimer.singleShot(0, self.check_portal)
        else:
            self.monitor_timer.stop()
            if self.worker is not None and self.automatic_attempt:
                self.worker.cancel()
            if self.worker is None:
                self.status.setText("静默自动登录已暂停。")
                if self.tray:
                    self.tray.setToolTip("校园网登录 · 自动登录已暂停")

    def check_portal(self) -> None:
        if self.quitting or not self.auto_login.isChecked() or self.worker is not None or self.probe_worker is not None:
            return
        if time.monotonic() - self.account_edited_at < 2.5:
            return
        try:
            self.saved_credentials()
            url = self.entry_url.text().strip()
            validate_url(url)
        except (LoginError, OSError, UnicodeError):
            self.status.setText("静默检测等待配置：请填写并保存账号信息。")
            return
        self.probe_worker = ProbeWorker(url, self)
        self.probe_worker.result.connect(self.on_probe_result)
        self.probe_worker.finished.connect(self.probe_finished)
        self.probe_worker.start()

    @Slot(bool)
    def on_probe_result(self, reachable: bool) -> None:
        if self.quitting or not self.auto_login.isChecked() or self.worker is not None:
            return
        # 配置立即写盘，但输入尚未停顿时不自动提交半截密码。
        if time.monotonic() - self.account_edited_at < 2.5:
            return
        was_reachable = self.policy.reachable
        due = self.policy.probe(reachable, time.monotonic())
        if not reachable and was_reachable is not False:
            self.status.setText("等待校园网入口，每 15 秒静默检测。")
            self.status.setStyleSheet("color: #6d7d93;")
            self.append_log("校园网入口暂不可达，继续后台检测。")
            if self.tray:
                self.tray.setToolTip("校园网登录 · 等待校园网入口")
        if due:
            self.start_login(automatic=True)

    def probe_finished(self) -> None:
        worker, self.probe_worker = self.probe_worker, None
        if worker is not None:
            worker.deleteLater()
        if self.close_pending:
            self.close()

    def apply_credentials(self, credentials: Credentials) -> None:
        operator = canonical_operator(credentials.operator)
        with QSignalBlocker(self.username), QSignalBlocker(self.password), QSignalBlocker(self.operator):
            self.username.setText(credentials.username)
            self.password.setText(credentials.password)
            self.operator.setCurrentIndex(self.operator.findText(operator))

    def credentials(self) -> Credentials:
        result = Credentials(self.username.text().strip(), self.password.text(), self.operator.currentText().strip())
        if any(not value.strip() for value in (result.username, result.password, result.operator)):
            raise LoginError("请填写账号、密码和运营商。")
        return result

    def append_log(self, message: str) -> None:
        self.log.appendPlainText(f"{datetime.now():%H:%M:%S}  {message}")

    def show_status(self, message: str, success: bool) -> None:
        self.status.setText(message)
        self.status.setStyleSheet("color: #208451;" if success else "color: #b34b3e;")
        self.append_log(message)
        if self.tray:
            self.tray.setToolTip("校园网登录 · " + ("已连接" if success else "请查看连接状态"))

    def toggle_advanced(self) -> None:
        visible = not self.advanced_panel.isVisible()
        self.advanced_panel.setVisible(visible)
        self.advanced_button.setText("收起高级设置" if visible else "高级设置")

    def import_account(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "导入账号文件", str(Path.home() / "Desktop"), "文本文件 (*.txt);;所有文件 (*)")
        if not filename:
            return
        try:
            self.apply_credentials(read_account_file(Path(filename)))
            if not self.autosave_account():
                return
            self.show_status("账号文件已导入并保存到本机。" if self.remember.isChecked() else "账号文件已导入。", True)
        except (LoginError, OSError, UnicodeError) as exc:
            message = str(exc) if isinstance(exc, LoginError) else "无法读取账号文件，请检查文件格式。"
            self.show_status(message, False)

    def save_account(self) -> None:
        try:
            save_config(self.config_path, self.credentials())
            self.remember.setChecked(True)
            self.persist_preferences()
            self.show_status("配置已保存到本机。", True)
            if self.background_services:
                QTimer.singleShot(0, self.check_portal)
        except LoginError as exc:
            self.show_status(str(exc), False)
        except OSError:
            self.show_status("无法保存配置，请确认程序文件夹可以写入。", False)

    def set_busy(self, busy: bool) -> None:
        for control in (self.username, self.password, self.operator, self.remember,
                        self.show_password, self.import_button, self.save_button,
                        self.login_button, self.advanced_panel):
            control.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        self.busy.setVisible(busy)
        self.login_button.setText("正在连接…" if busy else "连接校园网")

    def start_login(self, *, automatic: bool = False) -> None:
        if self.worker is not None or self.quitting:
            return
        try:
            credentials = self.saved_credentials() if automatic else self.credentials()
            url = self.entry_url.text().strip()
            validate_url(url)
            if not automatic and self.remember.isChecked():
                save_config(self.config_path, credentials)
        except LoginError as exc:
            self.show_status(str(exc), False)
            return
        except OSError:
            self.show_status("无法保存配置。请取消“自动保存到本机”后重试。", False)
            return
        self.persist_preferences()
        self.automatic_attempt = automatic
        self.log.clear()
        self.status.setText("正在静默连接校园网…" if automatic else "正在连接校园网…")
        self.status.setStyleSheet("color: #296df1;")
        self.set_busy(True)
        if self.tray:
            self.tray.setToolTip("校园网登录 · 正在连接")
        self.worker = LoginWorker(credentials, url=url, timeout=self.timeout.value(), headless=automatic or not self.show_browser.isChecked(), parent=self)
        self.worker.progress.connect(self.append_log)
        self.worker.result.connect(self.on_result)
        self.worker.finished.connect(self.worker_finished)
        self.worker.start()

    @Slot(bool, str)
    def on_result(self, success: bool, message: str) -> None:
        self.policy.completed(success, time.monotonic())
        self.show_status(message, success)
        if not success and self.automatic_attempt and self.auto_login.isChecked() and not self.quitting:
            if any(text in message for text in ("账号或密码错误", "用户名或密码错误", "密码不正确", "账户被锁", "验证码")):
                self.auto_login.setChecked(False)
                self.append_log("需要检查账号或完成验证，已暂停自动登录。")
            else:
                delay = max(1, round(self.policy.next_attempt - time.monotonic()))
                self.append_log(f"自动登录将在至少 {delay} 秒后重试。")

    def cancel_login(self) -> None:
        self.auto_login.setChecked(False)
        if self.worker is not None:
            self.worker.cancel()
            self.cancel_button.setEnabled(False)
            self.status.setText("正在取消，等待当前浏览器操作结束…")

    def worker_finished(self) -> None:
        worker, self.worker = self.worker, None
        self.set_busy(False)
        if worker is not None:
            worker.deleteLater()
        self.automatic_attempt = False
        if self.close_pending:
            self.close()

    @Slot()
    def request_exit(self) -> None:
        self.quitting = True
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.restore_timer.stop()
        self.activation_timer.stop()
        if not self.quitting and self.auto_login.isChecked() and self.tray is not None:
            self.hide()
            event.ignore()
            return
        self.quitting = True
        self.monitor_timer.stop()
        if self.tray_menu is not None:
            self.tray_menu.hide()
        if self.worker is not None:
            self.worker.cancel()
            self.status.setText("正在退出，等待浏览器关闭…")
        if self.worker is not None or self.probe_worker is not None:
            self.close_pending = True
            event.ignore()
        else:
            if self.tray:
                self.tray.hide()
            event.accept()
            if self.background_services:
                QApplication.instance().quit()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("校园网登录")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    if len(sys.argv) == 3 and sys.argv[1] == "--self-check":
        return self_check(app, Path(sys.argv[2]))
    shortcut_created = ensure_desktop_shortcut()
    instance = SingleInstance()
    if not instance.acquire(show_existing="--startup" not in sys.argv):
        return 0
    window = LoginWindow()
    instance.show_requested.connect(window.show_window)
    if not shortcut_created:
        window.append_log("桌面快捷方式未能创建，可继续使用程序。")
    if window.startup.isChecked():
        # 便携程序移动后从新位置运行，可更新已有启动项的路径。
        window.toggle_startup(True)
    background_ready = False
    if "--background" in sys.argv and window.auto_login.isChecked() and window.tray is not None:
        try:
            window.saved_credentials()
            background_ready = True
        except (LoginError, OSError, UnicodeError):
            pass
    if not background_ready:
        window.show()
    return app.exec()


def self_check(app: QApplication, output: Path) -> int:
    """验证发布包的 Qt 和浏览器驱动；不读取账号、不访问校园网。"""
    import json
    import tempfile
    from playwright.sync_api import sync_playwright
    result = {"qt": False, "edge": False, "config_beside_exe": False,
              "tray_menu": False, "tray_restore": False, "operator_select": False, "autosave": False}
    for key in ("CAMPUS_USERNAME", "CAMPUS_PASSWORD", "CAMPUS_OPERATOR"):
        os.environ.pop(key, None)
    try:
        with tempfile.TemporaryDirectory() as temp:
            window = LoginWindow(Path(temp) / ".env", background_services=False)
            window.show()
            app.processEvents()
            result["qt"] = window.isVisible() and window.password.echoMode() == QLineEdit.EchoMode.Password
            result["operator_select"] = not window.operator.isEditable() and window.operator.count() == len(OPERATORS)
            window.username.setText("packaged-test-user")
            window.password.setText("packaged-test-password")
            window.operator.setCurrentIndex(1)
            result["autosave"] = read_config(window.config_path)["CAMPUS_PASSWORD"] == "packaged-test-password"
            window.tray_menu = window.create_tray_menu()
            result["tray_menu"] = (window.tray_menu.actions()[0].text() == "打开窗口"
                                   and window.tray_menu.palette().color(QPalette.ColorRole.Window).name() == "#ffffff")
            def wait_for_restore():
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    app.processEvents()
                    if window.isVisible() and not window.isMinimized():
                        return True
                    time.sleep(.01)
                return False
            window.hide()
            window.tray_menu.actions()[0].trigger()
            hidden_restored = wait_for_restore()
            window.showMinimized()
            app.processEvents()
            window.tray_menu.actions()[0].trigger()
            result["tray_restore"] = hidden_restored and wait_for_restore()
            window.close()
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            try:
                page = browser.new_page()
                page.set_content('<input id="probe">')
                page.locator('#probe').fill('packaged-runtime-check')
                result["edge"] = page.locator('#probe').input_value() == 'packaged-runtime-check'
            finally:
                browser.close()
        result["config_beside_exe"] = ROOT == Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else True
    except Exception:
        pass
    output.write_text(json.dumps(result), encoding="utf-8")
    return 0 if all(result.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
