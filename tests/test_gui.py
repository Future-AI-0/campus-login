"""Qt 交互、后台线程与配置测试；使用临时配置和模拟认证。"""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from dotenv import dotenv_values
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit

from gui import LoginWindow, read_account_file, read_config, save_config, OPERATORS
from login import Credentials, LoginCancelled, LoginError


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / ".env"
        self.env_patch = patch.dict(os.environ, {"CAMPUS_USERNAME": "", "CAMPUS_PASSWORD": "", "CAMPUS_OPERATOR": ""})
        self.env_patch.start()
        self.window = LoginWindow(config_path=self.path, background_services=False)

    def tearDown(self):
        if self.window.worker is not None:
            self.window.worker.cancel()
            self.wait_until(lambda: self.window.worker is None)
        self.window.request_exit()
        if self.window.shortcut_worker is not None:
            self.wait_until(lambda: self.window.shortcut_worker is None)
        self.env_patch.stop()
        self.temp.cleanup()

    def wait_until(self, condition, timeout=4):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            QTest.qWait(20)
        self.assertTrue(condition(), "Qt 操作未按期完成")

    def fill(self):
        self.window.apply_credentials(Credentials("demo-user", "demo-password", "中国联通"))

    def test_password_masked_and_invalid_form_does_not_start(self):
        self.assertEqual(self.window.password.echoMode(), QLineEdit.EchoMode.Password)
        self.window.start_login()
        self.assertIsNone(self.window.worker)
        self.assertIn("请填写", self.window.status.text())

    def test_save_config_preserves_special_characters(self):
        credentials = Credentials("demo-user", "  p'\\a\\\\${UNSET_VALUE}#  ", "中国移动")
        save_config(self.path, credentials)
        stored = dotenv_values(self.path, interpolate=False)
        self.assertEqual(stored["CAMPUS_PASSWORD"], credentials.password)
        self.assertEqual(stored["CAMPUS_OPERATOR"], credentials.operator)

    def test_import_utf16_account_file(self):
        account_file = Path(self.temp.name) / "account.txt"
        account_file.write_text("demo-user|demo-password|中国电信\r\n", encoding="utf-16")
        credentials = read_account_file(account_file)
        self.assertEqual(credentials, Credentials("demo-user", "demo-password", "中国电信"))

    def test_operator_only_allows_fixed_choices_and_import_aliases(self):
        self.assertFalse(self.window.operator.isEditable())
        self.assertEqual(tuple(self.window.operator.itemText(i) for i in range(self.window.operator.count())), OPERATORS)
        self.window.apply_credentials(Credentials("demo", "password", "联通"))
        self.assertEqual(self.window.operator.currentText(), "中国联通")
        self.window.operator.setCurrentText("随意输入")
        self.assertEqual(self.window.operator.currentText(), "中国联通")
        with self.assertRaises(LoginError):
            self.window.apply_credentials(Credentials("demo", "password", "未知运营商"))

    def test_each_account_change_is_saved_immediately_including_empty(self):
        self.fill()
        self.window.username.setText("new-user")
        self.assertEqual(read_config(self.path)["CAMPUS_USERNAME"], "new-user")
        password = " new'\\${KEEP_LITERAL}# "
        self.window.password.setText(password)
        self.assertEqual(read_config(self.path)["CAMPUS_PASSWORD"], password)
        self.window.operator.setCurrentText("中国电信")
        self.assertEqual(read_config(self.path)["CAMPUS_OPERATOR"], "中国电信")
        self.window.password.clear()
        self.assertEqual(read_config(self.path)["CAMPUS_PASSWORD"], "")
        with self.assertRaises(LoginError):
            self.window.saved_credentials()
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_config_is_replaced_atomically_and_preserves_other_keys(self):
        self.path.write_text("OTHER_SETTING='keep'\n", encoding="utf-8")
        original = self.path.read_bytes()
        expected = Credentials("demo", "password", "中国移动")
        replace = os.replace
        final_commits = []
        def verify_complete_snapshot(source, destination):
            if Path(destination) != self.path:
                return replace(source, destination)
            final_commits.append(True)
            self.assertEqual(Path(destination).read_bytes(), original)
            values = dotenv_values(source, interpolate=False)
            self.assertEqual(values["CAMPUS_USERNAME"], expected.username)
            self.assertEqual(values["CAMPUS_PASSWORD"], expected.password)
            self.assertEqual(values["CAMPUS_OPERATOR"], expected.operator)
            self.assertEqual(values["OTHER_SETTING"], "keep")
            replace(source, destination)
        with patch("gui.os.replace", side_effect=verify_complete_snapshot):
            save_config(self.path, expected)
        self.assertEqual(len(final_commits), 1)

    def test_saved_gui_config_takes_precedence_over_old_environment(self):
        with patch.dict(os.environ, {"CAMPUS_USERNAME": "old-user", "CAMPUS_PASSWORD": "old-password", "CAMPUS_OPERATOR": "移动"}):
            self.assertEqual(read_config(self.path)["CAMPUS_USERNAME"], "old-user")
            self.fill()
            self.window.password.setText("new-password")
            self.assertEqual(self.window.saved_credentials(), Credentials("demo-user", "new-password", "中国联通"))

    def test_import_saves_complete_account_without_extra_click(self):
        account_file = Path(self.temp.name) / "account.txt"
        account_file.write_text("demo-user|demo-password|联通", encoding="utf-8")
        with patch("gui.QFileDialog.getOpenFileName", return_value=(str(account_file), "")):
            self.window.import_account()
        self.assertEqual(self.window.saved_credentials(), Credentials("demo-user", "demo-password", "中国联通"))

    def test_autosave_can_be_disabled_and_resumed(self):
        self.fill()
        self.window.password.setText("saved-password")
        self.window.remember.setChecked(False)
        self.window.password.setText("temporary-password")
        self.assertEqual(read_config(self.path)["CAMPUS_PASSWORD"], "saved-password")
        self.window.remember.setChecked(True)
        self.assertEqual(read_config(self.path)["CAMPUS_PASSWORD"], "temporary-password")

    def test_automatic_login_waits_while_account_is_being_edited(self):
        self.fill()
        self.window.password.setText("new-password")
        with patch("gui.run_login", return_value="校园网已连接。") as run, patch("gui.probe_reachable") as probe:
            self.window.check_portal()
            self.window.on_probe_result(True)
            run.assert_not_called()
            probe.assert_not_called()
            self.window.account_edited_at -= 3
            self.window.on_probe_result(True)
            self.wait_until(lambda: self.window.worker is None)
            self.assertEqual(run.call_args.args[0].password, "new-password")

    def test_startup_toggle_applies_and_rolls_back_on_failure(self):
        with patch("gui.set_startup") as apply:
            self.window.startup.setChecked(True)
            apply.assert_called_once_with(True)
            self.window.startup.setChecked(False)
            self.assertEqual(apply.call_args.args, (False,))
        with patch("gui.set_startup", side_effect=OSError()), patch("gui.startup_enabled", return_value=False):
            self.window.startup.setChecked(True)
        self.assertFalse(self.window.startup.isChecked())
        self.assertIn("无法修改", self.window.status.text())

    def test_shortcut_creation_does_not_block_ui_and_exit_waits(self):
        def fake_shortcut():
            time.sleep(.2)
            return True
        self.window.show()
        with patch('gui.ensure_desktop_shortcut', side_effect=fake_shortcut):
            started = time.monotonic()
            self.window.update_desktop_shortcut()
            self.assertLess(time.monotonic()-started, .1)
            self.assertTrue(self.window.isVisible())
            self.assertTrue(self.window.login_button.isEnabled())
            self.window.request_exit()
            self.assertTrue(self.window.close_pending)
            self.wait_until(lambda: self.window.shortcut_worker is None)
            self.wait_until(lambda: not self.window.isVisible())

    def test_login_runs_without_blocking_and_buttons_recover(self):
        self.fill()
        def fake_login(credentials, **kwargs):
            kwargs["progress"]("正在验证…")
            time.sleep(.25)
            return "校园网已连接。"
        with patch("gui.run_login", side_effect=fake_login):
            self.window.start_login()
            self.assertFalse(self.window.login_button.isEnabled())
            self.assertTrue(self.window.cancel_button.isEnabled())
            self.wait_until(lambda: self.window.worker is None)
        self.assertTrue(self.window.login_button.isEnabled())
        self.assertIn("已连接", self.window.status.text())
        self.assertIn("正在验证", self.window.log.toPlainText())

    def test_cancel_worker(self):
        self.fill()
        def fake_login(credentials, **kwargs):
            while not kwargs["cancelled"]():
                time.sleep(.01)
            raise LoginCancelled("已取消登录。")
        with patch("gui.run_login", side_effect=fake_login):
            self.window.start_login()
            self.window.cancel_login()
            self.wait_until(lambda: self.window.worker is None)
        self.assertIn("已取消", self.window.status.text())

    def test_close_waits_for_worker_cleanup(self):
        self.fill()
        self.window.show()
        def fake_login(credentials, **kwargs):
            while not kwargs["cancelled"]():
                time.sleep(.01)
            time.sleep(.15)
            raise LoginCancelled("已取消登录。")
        with patch("gui.run_login", side_effect=fake_login):
            self.window.start_login()
            self.window.close()
            self.assertTrue(self.window.close_pending)
            self.wait_until(lambda: self.window.worker is None)
            self.wait_until(lambda: not self.window.isVisible())

    def test_error_message_redacts_credentials(self):
        self.fill()
        with patch("gui.run_login", side_effect=LoginError("demo-user demo-password 操作失败")):
            self.window.start_login()
            self.wait_until(lambda: self.window.worker is None)
        self.assertNotIn("demo-password", self.window.log.toPlainText())
        self.assertNotIn("demo-user", self.window.status.text())

    def prepare_saved_account(self):
        credentials = Credentials("saved-user", "saved-password", "中国联通")
        save_config(self.path, credentials)
        for key in ("CAMPUS_USERNAME", "CAMPUS_PASSWORD", "CAMPUS_OPERATOR"):
            os.environ.pop(key, None)
        # 用户关闭自动保存时，草稿不覆盖后台使用的已保存配置。
        self.window.remember.setChecked(False)
        self.window.apply_credentials(Credentials("unsaved-user", "unsaved-password", "中国移动"))
        return credentials

    def test_reachable_automatically_logs_in_headless_once_with_saved_config(self):
        saved = self.prepare_saved_account()
        self.window.show_browser.setChecked(True)
        def fake_login(credentials, **kwargs):
            time.sleep(.1)
            return "校园网已连接。"
        with patch("gui.run_login", side_effect=fake_login) as run:
            self.window.on_probe_result(True)
            self.window.on_probe_result(True)
            self.wait_until(lambda: self.window.worker is None)
            self.window.on_probe_result(True)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], saved)
        self.assertTrue(run.call_args.kwargs["headless"])
        self.assertEqual(dotenv_values(self.path)["CAMPUS_USERNAME"], "saved-user")

    def test_probe_runs_in_background_and_only_one_probe_at_a_time(self):
        self.prepare_saved_account()
        def fake_probe(url):
            time.sleep(.15)
            return False
        with patch("gui.probe_reachable", side_effect=fake_probe) as probe:
            self.window.check_portal()
            self.window.check_portal()
            self.assertIsNotNone(self.window.probe_worker)
            self.assertTrue(self.window.login_button.isEnabled())
            self.wait_until(lambda: self.window.probe_worker is None)
        self.assertEqual(probe.call_count, 1)
        self.assertIn("等待校园网入口", self.window.status.text())

    def test_pause_ignores_late_probe_and_cancel_disables_auto(self):
        self.prepare_saved_account()
        self.window.auto_login.setChecked(False)
        with patch("gui.run_login") as run:
            self.window.on_probe_result(True)
        run.assert_not_called()
        self.fill()
        self.window.auto_login.setChecked(True)
        def fake_login(credentials, **kwargs):
            while not kwargs["cancelled"]():
                time.sleep(.01)
            raise LoginCancelled("已取消登录。")
        with patch("gui.run_login", side_effect=fake_login):
            self.window.start_login()
            self.window.cancel_login()
            self.wait_until(lambda: self.window.worker is None)
        self.assertFalse(self.window.auto_login.isChecked())

    def test_bad_password_pauses_auto_and_does_not_repeat(self):
        self.prepare_saved_account()
        with patch("gui.run_login", side_effect=LoginError("门户提示：账号或密码错误")) as run:
            self.window.on_probe_result(True)
            self.wait_until(lambda: self.window.worker is None)
            self.window.on_probe_result(True)
        self.assertEqual(run.call_count, 1)
        self.assertFalse(self.window.auto_login.isChecked())
        self.assertIn("已暂停自动登录", self.window.log.toPlainText())

    def test_close_hides_to_tray_and_exit_waits_for_probe(self):
        self.prepare_saved_account()
        self.window.tray = MagicMock()
        self.window.show()
        self.window.close()
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.window.quitting)
        def fake_probe(url):
            time.sleep(.2)
            return False
        with patch("gui.probe_reachable", side_effect=fake_probe):
            self.window.check_portal()
            self.window.request_exit()
            self.assertTrue(self.window.close_pending)
            self.wait_until(lambda: self.window.probe_worker is None)
        self.assertTrue(self.window.quitting)
        self.window.tray.hide.assert_called_once()
        self.window.tray = None

    def install_test_tray_menu(self):
        self.window.tray = MagicMock()
        self.window.tray_menu = self.window.create_tray_menu()
        self.window.tray.contextMenu.return_value = self.window.tray_menu
        return self.window.tray_menu

    def test_tray_menu_mouse_click_opens_hidden_window(self):
        menu = self.install_test_tray_menu()
        self.window.hide()
        menu.popup(QPoint(30, 30))
        QTest.qWait(40)
        action = next(action for action in menu.actions() if action.text() == "打开窗口")
        QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(action).center())
        self.wait_until(lambda: self.window.isVisible())
        self.assertFalse(menu.isVisible())
        self.assertFalse(self.window.isMinimized())
        self.window.close()
        self.assertFalse(self.window.isVisible())
        action.trigger()
        self.wait_until(lambda: self.window.isVisible())

    def test_tray_open_restores_minimized_window(self):
        menu = self.install_test_tray_menu()
        self.window.showMinimized()
        QTest.qWait(30)
        self.assertTrue(self.window.isMinimized())
        menu.actions()[0].trigger()
        self.wait_until(lambda: self.window.isVisible() and not self.window.isMinimized())

    def test_tray_menu_has_readable_colors_with_dark_system_palette(self):
        original = self.app.palette()
        dark = QPalette(original)
        dark.setColor(QPalette.ColorRole.Window, QColor("#202020"))
        dark.setColor(QPalette.ColorRole.WindowText, QColor("#ffffff"))
        try:
            self.app.setPalette(dark)
            menu = self.install_test_tray_menu()
            menu.popup(QPoint(30, 30))
            QTest.qWait(30)
            menu.setActiveAction(None)
            image = menu.grab().toImage()
            self.assertEqual(image.pixelColor(6, 6).name(), "#ffffff")
            text_color = menu.palette().color(QPalette.ColorRole.WindowText)
            self.assertLess(text_color.lightnessF(), .3)
            painted_text = sum(image.pixelColor(x, y) == text_color for y in range(image.height()) for x in range(image.width()))
            self.assertGreater(painted_text, 10, "菜单未以清晰的深色绘制文字")
            action = menu.actions()[0]
            menu.setActiveAction(action)
            image = menu.grab().toImage()
            rect = menu.actionGeometry(action)
            point = QPoint(rect.right() - 6, rect.center().y())
            self.assertEqual(image.pixelColor(point).name(), "#296df1")
            self.assertEqual(menu.palette().color(QPalette.ColorRole.HighlightedText).name(), "#ffffff")
        finally:
            self.app.setPalette(original)

    def test_exit_cancels_pending_tray_open(self):
        menu = self.install_test_tray_menu()
        self.window.hide()
        menu.actions()[0].trigger()
        self.window.request_exit()
        QTest.qWait(200)
        self.assertTrue(self.window.quitting)
        self.assertFalse(self.window.isVisible())


if __name__ == "__main__":
    unittest.main()
