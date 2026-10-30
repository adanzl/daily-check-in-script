"""新界签到模块测试。不联网。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from daily_checkin.configs import checkin_map
from daily_checkin.neworld.main import Neworld, fingerprint_for, parse_remaining_traffic


class FakeResponse:
    def __init__(self, payload=None, text="", status_code=200):
        self._payload = payload
        self.text = text
        self.status_code = status_code

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.calls = []

    def post(self, url, json=None, data=None, headers=None, timeout=None):
        self.calls.append(("POST", url, json, data))
        return self.routes[url]

    def get(self, url, timeout=None):
        self.calls.append(("GET", url))
        return self.routes[url]


def test_fingerprint_is_stable():
    assert fingerprint_for("a@b.c") == fingerprint_for("a@b.c")
    assert len(fingerprint_for("a@b.c")) == 32


def test_parse_remaining_traffic():
    assert parse_remaining_traffic("剩余流量 69.85GB") == "69.85GB"
    assert parse_remaining_traffic("没有") == ""


def test_missing_account_raises_before_network():
    try:
        Neworld({"email": "", "password": ""}).main()
    except RuntimeError as exc:
        assert "邮箱" in str(exc)
    else:
        raise AssertionError("应当因缺少账号失败")


def test_login_failure():
    session = FakeSession(
        {
            "https://neworld.cloud/auth/login": FakeResponse({"ret": 0, "msg": "邮箱或者密码错误"}),
        }
    )
    bot = Neworld({"email": "a@b.c", "password": "secret"})
    try:
        bot.login(session, "a@b.c", "secret", "")
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("登录失败应当抛出")
    assert "密码错误" in message
    assert "secret" not in message
    body = session.calls[0][2]
    assert body["passwd"] == "secret"
    assert body["fingerprint"] == fingerprint_for("a@b.c")


def test_already_checked_in_uses_page_traffic():
    session = FakeSession(
        {
            "https://neworld.cloud/auth/login": FakeResponse({"ret": 1, "msg": "登录成功，欢迎回来"}),
            "https://neworld.cloud/user/checkin": FakeResponse({"ret": 0, "msg": "今天已经签到过了"}),
            "https://neworld.cloud/user": FakeResponse(text="<div>剩余流量 69.85GB</div>"),
        }
    )
    bot = Neworld({"email": "a@b.c", "password": "secret", "totp": ""})
    bot._session = lambda: session
    message = bot.main()
    assert "今天已经签到过了" in message
    assert "剩余流量: 69.85GB" in message
    assert "secret" not in message
    checkin_call = session.calls[1]
    assert checkin_call[3]["checkin_type"] == "traffic"


def test_checkin_success_prefers_api_traffic():
    session = FakeSession(
        {
            "https://neworld.cloud/auth/login": FakeResponse({"ret": 1, "msg": "登录成功"}),
            "https://neworld.cloud/user/checkin": FakeResponse(
                {"ret": 1, "msg": "获得了 16MB 流量", "traffic": "10.02GB"}
            ),
        }
    )
    bot = Neworld({"email": "a@b.c", "password": "secret"})
    bot._session = lambda: session
    message = bot.main()
    assert "获得了 16MB 流量" in message
    assert "剩余流量: 10.02GB" in message
    assert all(call[0] != "GET" for call in session.calls)


def test_captcha_required_is_failure():
    session = FakeSession(
        {
            "https://neworld.cloud/user/checkin": FakeResponse({"ret": 2, "msg": ""}),
        }
    )
    bot = Neworld({"email": "a@b.c", "password": "secret"})
    try:
        bot.checkin(session, "traffic")
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("需要验证时应当失败")
    assert "人机验证" in message


def test_registered_in_checkin_map():
    name, cls = checkin_map["NEWORLD"]
    assert name == "新界"
    assert cls is Neworld


if __name__ == "__main__":
    failed = 0
    for item_name, item in sorted(globals().items()):
        if item_name.startswith("test_") and callable(item):
            try:
                item()
            except Exception as exc:
                failed += 1
                print(f"FAIL {item_name}: {exc}")
            else:
                print(f"ok {item_name}")
    if failed:
        raise SystemExit(1)
