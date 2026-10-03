"""Windows 桌面快捷方式和同一用户的单实例入口。"""
from __future__ import annotations

import base64
import hashlib
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket


def shortcut_launch() -> tuple[Path, list[str], Path]:
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        return executable, ["--background"], executable.parent
    root = Path(__file__).resolve().parent
    pythonw = root / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.exists():
        pythonw = Path(sys.executable).with_name("pythonw.exe")
    return pythonw, [str(root / "gui.py"), "--background"], root


def shortcut_script(target: Path, arguments: list[str], directory: Path) -> str:
    def quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"
    args = subprocess.list2cmdline(arguments)
    return f"""
$ErrorActionPreference = 'Stop'
$taskDesktop = [Environment]::GetFolderPath('DesktopDirectory')
if (-not $taskDesktop) {{ throw 'Desktop unavailable' }}
$taskShell = New-Object -ComObject WScript.Shell
$taskLinkPath = Join-Path $taskDesktop '校园网登录.lnk'
$taskLink = $taskShell.CreateShortcut($taskLinkPath)
$taskLink.TargetPath = {quote(str(target))}
$taskLink.Arguments = {quote(args)}
$taskLink.WorkingDirectory = {quote(str(directory))}
$taskLink.Description = '校园网静默检测与自动登录'
$taskLink.IconLocation = {quote(str(target) + ',0')}
$taskLink.Save()
"""


def ensure_desktop_shortcut() -> bool:
    if sys.platform != "win32":
        return False
    target, arguments, directory = shortcut_launch()
    encoded = base64.b64encode(shortcut_script(target, arguments, directory).encode("utf-16-le")).decode("ascii")
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=10, check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class SingleInstance(QObject):
    show_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        user = str(Path.home()).casefold().encode("utf-8")
        self.name = "CampusLogin-" + hashlib.sha256(user).hexdigest()[:16]
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.newConnection.connect(self.accept_connection)
        self.clients = []

    def contact_existing(self) -> bool:
        socket = QLocalSocket()
        socket.connectToServer(self.name)
        if not socket.waitForConnected(300):
            return False
        socket.write(b"show\n")
        socket.waitForBytesWritten(300)
        # Windows 命名管道需等服务端读取后再关闭，否则请求可能被丢弃。
        socket.waitForReadyRead(1500)
        socket.disconnectFromServer()
        return True

    def acquire(self) -> bool:
        if self.contact_existing():
            return False
        if self.server.listen(self.name):
            return True
        # 再检查一次，覆盖两个进程同时启动的情况。
        if self.contact_existing():
            return False
        QLocalServer.removeServer(self.name)
        return self.server.listen(self.name)

    def accept_connection(self) -> None:
        socket = self.server.nextPendingConnection()
        if socket is None:
            return
        self.clients.append(socket)
        socket.readyRead.connect(lambda: self.read_request(socket))
        socket.disconnected.connect(lambda: self.remove_client(socket))
        if socket.bytesAvailable():
            self.read_request(socket)

    def read_request(self, socket: QLocalSocket) -> None:
        if not socket.canReadLine():
            return
        if bytes(socket.readLine()).strip() == b"show":
            self.show_requested.emit()
        socket.write(b"ok\n")
        socket.flush()

    def remove_client(self, socket: QLocalSocket) -> None:
        if socket in self.clients:
            self.clients.remove(socket)
        socket.deleteLater()
