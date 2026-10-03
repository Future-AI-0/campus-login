"""入口探测、自动重试、偏好持久化与 Windows 启动辅助测试。"""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from PySide6.QtTest import QTest
from PySide6.QtCore import QProcess
from PySide6.QtWidgets import QApplication

from background import AutoLoginPolicy, PortalRedirectHandler, load_preferences, probe_reachable, save_preferences
from desktop import SingleInstance, shortcut_script
from login import PORTAL_URL


class BackgroundTests(unittest.TestCase):
    def test_failure_backoff_and_reconnect(self):
        policy = AutoLoginPolicy()
        self.assertFalse(policy.probe(False, 100))
        self.assertTrue(policy.probe(True, 101))
        for index, delay in enumerate((60, 120, 240, 300, 300)):
            now = 200 + index * 1000
            policy.completed(False, now)
            self.assertFalse(policy.probe(True, now + delay - 1))
            self.assertTrue(policy.probe(True, now + delay))
        self.assertFalse(policy.probe(False, 5001))
        self.assertTrue(policy.probe(True, 5002))
        self.assertEqual(policy.failures, 0)

    def test_success_does_not_relogin_on_every_probe(self):
        policy = AutoLoginPolicy()
        policy.completed(True, 100)
        self.assertFalse(policy.probe(True, 115))
        self.assertFalse(policy.probe(True, 399))
        self.assertTrue(policy.probe(True, 400))

    def test_probe_accepts_portal_response_and_handles_timeout(self):
        response = MagicMock(status=200, url=PORTAL_URL)
        response.__enter__.return_value = response
        opener = MagicMock()
        opener.open.return_value = response
        with patch("background.build_opener", return_value=opener):
            self.assertTrue(probe_reachable(PORTAL_URL))
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, PORTAL_URL)
            self.assertIsNone(request.data)
            opener.open.side_effect = URLError("timeout")
            self.assertFalse(probe_reachable(PORTAL_URL))

    def test_probe_rejects_error_and_unrelated_page(self):
        response = MagicMock(status=200, url="https://example.org/")
        response.__enter__.return_value = response
        opener = MagicMock()
        opener.open.return_value = response
        with patch("background.build_opener", return_value=opener):
            self.assertFalse(probe_reachable(PORTAL_URL))
            response.url, response.status = PORTAL_URL, 555
            self.assertFalse(probe_reachable(PORTAL_URL))
        handler = PortalRedirectHandler()
        with self.assertRaises(HTTPError):
            handler.redirect_request(Request(PORTAL_URL), None, 302, "Found", {}, "https://example.org/login")

    def test_preferences_restore_and_ignore_corrupt_data(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            save_preferences(path, {"auto_login": False, "entry_url": PORTAL_URL, "timeout": 90, "show_browser": False})
            self.assertEqual(load_preferences(path)["timeout"], 90)
            self.assertFalse(load_preferences(path)["auto_login"])
            path.write_text("broken", encoding="utf-8")
            self.assertTrue(load_preferences(path)["auto_login"])
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_shortcut_quotes_paths_and_uses_one_desktop_filename(self):
        target = Path("C:/用户/O'Brien/App $() Folder/pythonw.exe")
        script = shortcut_script(target, ["C:/用户/O'Brien/Program Files/gui.py", "--background"], target.parent)
        self.assertIn("O''Brien", script)
        self.assertIn('"C:/用户/O\'\'Brien/Program Files/gui.py" --background', script)
        self.assertIn("GetFolderPath('DesktopDirectory')", script)
        self.assertIn("'校园网登录.lnk'", script)
        self.assertEqual(script.count(".Save()"), 1)

    def test_shortcut_runs_hidden_without_credentials(self):
        with patch("desktop.sys.platform", "win32"), patch("desktop.subprocess.run") as run:
            run.return_value.returncode = 0
            from desktop import ensure_desktop_shortcut
            self.assertTrue(ensure_desktop_shortcut())
            self.assertEqual(run.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
            self.assertEqual(run.call_args.kwargs["stdout"], subprocess.DEVNULL)

    def test_second_instance_requests_existing_window(self):
        app = QApplication.instance() or QApplication([])
        first = SingleInstance()
        first.name = "CampusLogin-test-" + uuid.uuid4().hex
        received = []
        first.show_requested.connect(lambda: received.append(True))
        try:
            self.assertTrue(first.acquire())
            child = QProcess()
            child.setWorkingDirectory(str(Path(__file__).resolve().parent.parent))
            code = ("from PySide6.QtCore import QCoreApplication; from desktop import SingleInstance; "
                    f"app=QCoreApplication([]); instance=SingleInstance(); instance.name={first.name!r}; "
                    "raise SystemExit(1 if instance.acquire() else 0)")
            child.start(sys.executable, ["-c", code])
            for _ in range(150):
                QTest.qWait(20)
                if child.state() == QProcess.ProcessState.NotRunning:
                    break
            self.assertEqual(child.state(), QProcess.ProcessState.NotRunning)
            self.assertEqual(child.exitCode(), 0)
            self.assertEqual(received, [True])
        finally:
            first.server.close()


if __name__ == "__main__":
    unittest.main()
