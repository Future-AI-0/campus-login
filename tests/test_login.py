"""用本地路由模拟真实门户结构，不向校园网发送测试账号。"""
from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import sync_playwright

from login import (
    BrowserError,
    Credentials,
    LoginError,
    LoginPending,
    PORTAL_URL,
    automate,
    prepare_form,
    run_login,
)

FORM = """
<input id="nameInput" name="username" placeholder="请输入账号">
<input type="password" placeholder="请输入密码">
<input type="hidden" name="execution" value="token">
<label><input type="checkbox">记住密码</label>
<label class="protocol__track"><input id="consent" type="checkbox">请先阅读并同意</label>
<button id="submitBtn">立即登录</button>
"""
SERVICES = """
<app-service-selection>
  <div id="relationInfo">
    <div class="service-box">中国移动</div>
    <div class="service-box">中国联通</div>
    <div class="service-box">中国电信</div>
  </div>
  <button id="confirm" disabled>确定</button>
</app-service-selection>
"""


class LoginConnectivityTests(unittest.TestCase):
    credentials = Credentials('test-user', 'test-password', '中国电信')

    def test_verified_internet_skips_browser_and_credentials(self):
        with patch('network.internet_available', return_value=True), patch('login.sync_playwright') as browser:
            self.assertIn('无需重复登录', run_login(self.credentials))
            browser.assert_not_called()

    def test_portal_online_without_external_proof_is_pending_and_browser_closed(self):
        with patch('network.internet_available', return_value=None), patch('login.sync_playwright') as factory, patch('login.automate', return_value='校园网已连接。'):
            browser = factory.return_value.__enter__.return_value.chromium.launch.return_value
            with self.assertRaises(LoginPending):
                run_login(self.credentials)
            browser.close.assert_called_once()

    def test_portal_555_without_external_proof_is_unknown_not_offline(self):
        with patch('network.internet_available', return_value=None), patch('login.sync_playwright'), patch('login.automate', side_effect=LoginError('HTTP 555')):
            with self.assertRaisesRegex(LoginPending, '状态待确认'):
                run_login(self.credentials)

    def test_post_login_requires_external_proof_and_dry_run_does_not_probe(self):
        with patch('network.internet_available', side_effect=[None, True]), patch('login.sync_playwright'), patch('login.automate', return_value='校园网已连接。'):
            self.assertIn('HTTPS 验证通过', run_login(self.credentials))
        with patch('network.internet_available') as verify, patch('login.sync_playwright'), patch('login.automate', return_value='演练完成'):
            self.assertEqual(run_login(self.credentials, dry_run=True), '演练完成')
            verify.assert_not_called()


class PortalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            channel="msedge", headless=True, args=["--no-proxy-server"]
        )

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.submissions = []
        self.credentials = Credentials("test-user", "test-password", "联通")

    def tearDown(self):
        self.context.close()

    def serve(self, *, login_result="success", delayed=False, already_online=False, duplicate=False, conflicting_online=False):
        services_script = """
          window.showServices = () => {
            const show = () => {
              document.body.innerHTML = SERVICES;
              for (const card of document.querySelectorAll('.service-box')) {
                card.onclick = () => {
                  window.selected = card.textContent;
                  document.querySelector('#confirm').disabled = false;
                };
              }
              document.querySelector('#confirm').onclick = async () => {
                const result = await fetch('/eportal/network/serviceLogin', {
                  method:'POST', body:JSON.stringify({operator:window.selected})
                }).then(r=>r.json());
                await fetch('/eportal/adaptor/getOnlineUserInfo');
                if (result.data.authResult === 'success') {
                  history.pushState({}, '', '/portal/entry/pc/loginSuccess');
                  document.body.textContent='登录成功';
                }
              };
            };
            setTimeout(show, DELAY);
          };
        """.replace("SERVICES", json.dumps(SERVICES)).replace("DELAY", "600" if delayed else "0")
        def route_handler(route):
            path = route.request.url.split("10.254.241.66", 1)[-1].split("?", 1)[0]
            if path.startswith("/portal/entry/pc/authenticate"):
                html = '<iframe src="/cas-sso/login"></iframe><script>' + services_script + '</script>'
                if already_online:
                    html = '<script>fetch("/eportal/adaptor/getOnlineUserInfo")</script>'
                route.fulfill(content_type="text/html; charset=utf-8", body=html)
            elif path == "/cas-sso/login":
                form = FORM + ('<input name="username">' if duplicate else '')
                script = """
                  document.querySelector('#submitBtn').onclick = async () => {
                    await fetch('/test/credentials', {method:'POST', body:JSON.stringify({
                      username: document.querySelector('#nameInput').value,
                      password: document.querySelector('input[type=password]').value,
                      consent: document.querySelector('#consent').checked
                    })});
                    window.top.showServices();
                  };
                """
                route.fulfill(content_type="text/html; charset=utf-8", body=form + '<script>' + script + '</script>')
            elif path == "/test/credentials":
                self.submissions.append(route.request.post_data_json)
                route.fulfill(json={"ok": True})
            elif path == "/eportal/network/serviceLogin":
                self.submissions.append(route.request.post_data_json)
                route.fulfill(json={"code": 200, "data": {"authResult": login_result}})
            elif path == "/eportal/adaptor/getOnlineUserInfo":
                positive = already_online or (bool(self.submissions) and (login_result == "success" or conflicting_online))
                route.fulfill(json={"code": 200, "data": {"portalOnlineUserInfo": {"result": "success" if positive else "fail"}}})
            else:
                route.fulfill(status=404)
        self.context.route("**/*", route_handler)

    def test_iframe_login_and_exact_operator(self):
        self.serve(delayed=True)
        result = automate(self.page, self.credentials, 8, False, PORTAL_URL)
        self.assertEqual(result, "校园网已连接。")
        self.assertEqual(self.submissions, [
            {"username": "test-user", "password": "test-password", "consent": True},
            {"operator": "中国联通"},
        ])

    def test_dry_run_never_submits(self):
        self.serve()
        result = automate(self.page, self.credentials, 5, True, PORTAL_URL)
        self.assertIn("没有提交", result)
        self.assertEqual(self.submissions, [])
        frame = self.page.frames[1]
        self.assertTrue(frame.locator('#consent').is_checked())
        self.assertFalse(frame.get_by_role('checkbox', name='记住密码').is_checked())

    def test_already_online_without_login_form(self):
        self.serve(already_online=True)
        result = automate(self.page, self.credentials, 5, False, PORTAL_URL)
        self.assertIn("无需重复登录", result)
        self.assertEqual(self.submissions, [])

    def test_wrong_operator_never_confirms(self):
        self.serve()
        credentials = Credentials("test-user", "test-password", "不存在的运营商")
        with self.assertRaisesRegex(LoginError, "当前选项"):
            automate(self.page, credentials, 5, False, PORTAL_URL)
        self.assertEqual(len(self.submissions), 1)

    def test_operator_authentication_failure(self):
        self.serve(login_result="fail")
        with self.assertRaisesRegex(LoginError, "运营商认证失败"):
            automate(self.page, self.credentials, 5, False, PORTAL_URL)
        self.assertEqual(len(self.submissions), 2)

    def test_operator_failure_takes_precedence_over_campus_online(self):
        self.serve(login_result="fail", conflicting_online=True)
        with self.assertRaisesRegex(LoginError, "运营商认证失败"):
            automate(self.page, self.credentials, 5, False, PORTAL_URL)
        self.assertEqual(len(self.submissions), 2)

    def test_duplicate_visible_username_stops(self):
        self.serve(duplicate=True)
        with self.assertRaisesRegex(LoginError, "多个可见控件"):
            automate(self.page, self.credentials, 5, False, PORTAL_URL)
        self.assertEqual(self.submissions, [])

    def test_http_200_without_success_does_not_connect(self):
        self.context.route("**/*", lambda route: route.fulfill(content_type="text/html", body="网络连接中..."))
        with self.assertRaisesRegex(LoginError, "未确认连接成功"):
            automate(self.page, self.credentials, 1, False, PORTAL_URL)

    def test_terminal_555_does_not_override_explicit_offline(self):
        def handler(route):
            if 'queryTerminalInfo' in route.request.url:
                route.fulfill(status=555)
            elif 'getOnlineUserInfo' in route.request.url:
                route.fulfill(json={'data':{'portalOnlineUserInfo':{'result':'fail'}}})
            else:
                route.fulfill(content_type='text/html', body='''网络连接中...
                  <script>fetch('/eportal/adaptor/getOnlineUserInfo');
                  setInterval(()=>fetch('/eportal/adaptor/queryTerminalInfo'),100)</script>''')
        self.context.route('**/*', handler)
        with self.assertRaisesRegex(LoginError, 'HTTP 555'):
            automate(self.page, self.credentials, 4, False, PORTAL_URL)
        self.assertEqual(self.submissions, [])

    def test_entry_555_without_online_proof_does_not_connect(self):
        self.context.route('**/*', lambda route: route.fulfill(status=555, body="已登录"))
        with self.assertRaisesRegex(LoginError, 'HTTP 555'):
            automate(self.page, self.credentials, 1, False, PORTAL_URL)

    def test_555_with_explicit_online_success_still_skips_login(self):
        def handler(route):
            if 'getOnlineUserInfo' in route.request.url:
                route.fulfill(json={'data':{'portalOnlineUserInfo':{'result':'success'}}})
            else:
                route.fulfill(status=555, content_type='text/html', body="<script>fetch('/eportal/adaptor/getOnlineUserInfo')</script>")
        self.context.route('**/*', handler)
        self.assertIn("无需重复登录", automate(self.page, self.credentials, 4, False, PORTAL_URL))
        self.assertEqual(self.submissions, [])

    def test_expired_entry_refreshes_once_and_completes_login(self):
        self.serve()
        fresh = PORTAL_URL + '?fresh=1'
        self.context.route(PORTAL_URL, lambda route: route.fulfill(status=555, headers={'Location':'http://123.123.123.123'}, body='刷新入口'))
        resolver = Mock(return_value=fresh)
        changed = Mock()
        self.assertEqual(automate(self.page, self.credentials, 8, False, PORTAL_URL, entry_resolver=resolver, entry_changed=changed), "校园网已连接。")
        resolver.assert_called_once()
        changed.assert_called_once_with(fresh)
        self.assertEqual(len(self.submissions), 2)

    def test_refreshed_entry_must_still_be_trusted(self):
        self.context.route('**/*', lambda route: route.fulfill(status=555, content_type='text/html', body='刷新入口'))
        resolver = Mock(return_value='http://example.test/portal/login')
        with self.assertRaisesRegex(LoginError, '入口必须'):
            automate(self.page, self.credentials, 4, False, PORTAL_URL, entry_resolver=resolver)
        self.assertEqual(self.submissions, [])

    def test_sso_frame_detaches_after_submission_without_duplicate_login(self):
        self.serve(delayed=True)
        from login import check_failure
        detached = []
        def transient_failure(page, status):
            if self.submissions and not detached:
                detached.append(True)
                raise BrowserError('Locator.count: Frame was detached')
            return check_failure(page, status)
        with patch('login.check_failure', side_effect=transient_failure):
            self.assertEqual(automate(self.page, self.credentials, 8, False, PORTAL_URL), "校园网已连接。")
        self.assertTrue(detached)
        self.assertEqual(len(self.submissions), 2)

    def test_unrelated_555_does_not_mark_online(self):
        def handler(route):
            if route.request.url.endswith('/tracking'):
                route.fulfill(status=555)
            else:
                route.fulfill(content_type='text/html', body='''网络连接中...
                  <script>fetch('/tracking');fetch('http://example.test/tracking')</script>''')
        self.context.route('**/*', handler)
        with self.assertRaisesRegex(LoginError, '未确认连接成功'):
            automate(self.page, self.credentials, 1, False, PORTAL_URL)

    def test_service_555_alone_does_not_confirm_connection(self):
        self.serve(login_result="fail")
        self.context.route('**/eportal/network/serviceLogin', lambda route: route.fulfill(status=555, json={}))
        with self.assertRaisesRegex(LoginError, 'HTTP 555'):
            automate(self.page, self.credentials, 2, False, PORTAL_URL)

    def test_agreement_already_checked_stays_checked(self):
        self.context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=FORM))
        self.page.goto(PORTAL_URL)
        self.page.locator('#consent').check()
        prepare_form(self.page.main_frame, self.credentials)
        self.assertTrue(self.page.locator('#consent').is_checked())

    def test_untrusted_iframe_does_not_receive_credentials(self):
        self.context.route('**/*', lambda route: route.fulfill(content_type='text/html', body='<iframe src="http://example.test/login"></iframe>' if '10.254.241.66' in route.request.url else FORM))
        with self.assertRaisesRegex(LoginError, '未确认连接成功'):
            automate(self.page, self.credentials, 1, False, PORTAL_URL)
        self.assertEqual(self.page.frames[1].locator('#nameInput').input_value(), '')


if __name__ == "__main__":
    unittest.main()
