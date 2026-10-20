"""Bilibili 签到模块回归测试。

不联网：所有 HTTP 调用都走 FakeSession。
跑法：`python -m pytest tests/ -q` 或直接 `python tests/test_bilibili.py`。
"""

import http.cookiejar
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    """按 URL 关键字路由到固定响应。"""

    def __init__(self, router=None):
        self.router = router or {}
        self.cookies = http.cookiejar.CookieJar()
        self.headers = {}

    def _do(self, url):
        for key, payload in self.router.items():
            if key in url:
                return FakeResp(payload)
        return FakeResp({"code": -1, "message": f"未路由: {url}"})

    def get(self, url, **kwargs):
        return self._do(url)

    def post(self, url, **kwargs):
        return self._do(url)


def _bilibili():
    # 延迟导入：daily_checkin/__init__.py 会 import 全部模块，需要装好依赖
    from daily_checkin.bilibili.main import BiliBili

    return BiliBili


# ---------------------------------------------------------------- data: null
def test_today_exp_handles_null_data():
    BiliBili = _bilibili()
    assert BiliBili.get_today_exp(FakeSession({"exp/log": {"code": 0, "data": None}})) == []
    assert BiliBili.get_today_exp(FakeSession({"exp/log": {"code": 0, "data": {"list": None}}})) == []


def test_get_nav_handles_null_data():
    BiliBili = _bilibili()
    session = FakeSession({"web-interface/nav": {"code": -101, "message": "账号未登录", "data": None}})
    assert BiliBili.get_nav(session) == (None, None, None, None, None, None)


def test_live_status_handles_null_data():
    BiliBili = _bilibili()
    session = FakeSession({"Exchange/getStatus": {"code": 0, "data": None}})
    names = {one["name"]: one["value"] for one in BiliBili.live_status(session)}
    assert names == {"硬币数量": 0, "金瓜子数": 0, "银瓜子数": 0}


# ------------------------------------------------------------ 漫画 / 直播签到
def test_manga_sign_chinese_duplicate_is_not_failure():
    BiliBili = _bilibili()
    msg = BiliBili.manga_sign(FakeSession({"ClockIn": {"code": 75000, "msg": "不能重复签到~"}}))
    assert "已经签到" in msg
    assert "失败" not in msg


def test_manga_sign_real_error_still_reports_failure():
    BiliBili = _bilibili()
    msg = BiliBili.manga_sign(FakeSession({"ClockIn": {"code": 75001, "msg": "未知错误"}}))
    assert "失败" in msg


def test_live_sign_offline_activity_is_not_failure():
    BiliBili = _bilibili()
    msg = BiliBili.live_sign(FakeSession({"DoSign": {"code": 1, "message": "签到活动已下线，无法使用"}}))
    assert "已下线" in msg
    assert "失败" not in msg


def test_live_sign_success():
    BiliBili = _bilibili()
    session = FakeSession({"DoSign": {"code": 0, "data": {"text": "3经验", "specialText": "", "hadSignDays": 5}}})
    assert "签到成功" in BiliBili.live_sign(session)


# ------------------------------------------------------------------- 分享任务
def test_share_71000_normalised_to_success():
    BiliBili = _bilibili()
    ret = BiliBili.share_task(FakeSession({"share/add": {"code": 71000, "message": "重复"}}), "jct", 123)
    assert ret["code"] == 0
    assert ret["duplicate"] is True


def test_share_real_error_preserved():
    BiliBili = _bilibili()
    ret = BiliBili.share_task(FakeSession({"share/add": {"code": -111, "message": "csrf校验失败"}}), "jct", 123)
    assert ret["code"] == -111


# ------------------------------------------------------------------- 观看上报
def test_report_task_skipped_by_default():
    """默认不上报，避免污染观看历史。"""
    BiliBili = _bilibili()
    ret = BiliBili.report_task(FakeSession({"history/report": {"code": 0}}), "jct", 1, 2)
    assert ret["skipped"] is True


def test_report_task_runs_when_config_enabled():
    """config.json 里 watch_video=true 时才真的上报。"""
    BiliBili = _bilibili()
    ret = BiliBili.report_task(FakeSession({"history/report": {"code": 0}}), "jct", 1, 2, enabled=True)
    assert ret["code"] == 0


def test_report_task_handles_non_json_response():
    BiliBili = _bilibili()
    session = FakeSession({"history/report": ValueError("not json")})
    ret = BiliBili.report_task(session, "jct", 1, 2, enabled=True)
    assert ret["code"] == -1


# ---------------------------------------------------------------- Cookie 解析
def test_parse_cookie_keeps_equals_and_skips_junk():
    BiliBili = _bilibili()
    cookie = BiliBili.parse_cookie("SESSDATA=ab%3Dc==; bili_jct=xyz; ; broken; k=")
    assert cookie["SESSDATA"] == "ab%3Dc=="
    assert cookie["bili_jct"] == "xyz"
    assert "broken" not in cookie


def test_parse_cookie_empty():
    BiliBili = _bilibili()
    assert BiliBili.parse_cookie("") == {}


# ------------------------------------------------------------------ 整体流程
def _happy_router():
    return {
        "web-interface/nav": {
            "code": 0,
            "data": {
                "isLogin": True,
                "uname": "tester",
                "mid": 1,
                "money": 10,
                "vipType": 1,
                # Lv6 封顶：经验已超过 28800
                "level_info": {"current_exp": 42604},
            },
        },
        "ClockIn": {"code": 75000, "msg": "不能重复签到~"},
        "DoSign": {"code": 0, "data": {"text": "3经验", "specialText": "", "hadSignDays": 5}},
        # ranking/v2 被风控 -> 应回落到 popular
        "ranking/v2": {"code": -352, "message": "风控校验失败"},
        "web-interface/popular": {
            "code": 0,
            "data": {"list": [{"aid": 1, "cid": 2, "title": "测试视频", "owner": {"name": "up"}}]},
        },
        "vip/privilege/my": {"code": 0, "data": {"list": []}},
        "exp/log": {
            "code": 0,
            "data": {"list": [{"reason": "视频投币奖励", "delta": 10, "time": "2026-10-20 09:00:00"}]},
        },
        "share/add": {"code": 71000, "message": "重复"},
        "history/report": {"code": 0},
        "card/followings": {"code": 0, "data": {"list": []}},
        "Exchange/getStatus": {"code": 0, "data": None},
    }


def _patch_session(monkeypatch_module, router):
    class _Session(FakeSession):
        def __init__(self):
            super().__init__(router)

    monkeypatch_module.session = _Session


def test_main_flow_caps_level_and_falls_back():
    import requests

    BiliBili = _bilibili()
    original = requests.session
    _patch_session(requests, _happy_router())
    try:
        out = BiliBili({"cookie": "SESSDATA=x; bili_jct=y", "coin_num": 0}).main()
    finally:
        requests.session = original

    assert "已封顶(Lv6)" in out, out
    assert "分享《测试视频》（今日已分享过）" in out, out
    assert "已关闭（watch_video=false" in out, out
    assert "硬币数量: 0" in out, out


def test_main_flow_empty_aid_list_does_not_crash():
    import requests

    BiliBili = _bilibili()
    router = _happy_router()
    router["web-interface/popular"] = {"code": 0, "data": {"list": []}}
    router["newlist"] = {"code": 0, "data": {"archives": []}}
    original = requests.session
    _patch_session(requests, router)
    try:
        out = BiliBili({"cookie": "SESSDATA=x; bili_jct=y", "coin_num": 0}).main()
    finally:
        requests.session = original

    assert "已跳过（没拿到视频列表）" in out, out


def test_main_flow_raises_on_expired_cookie():
    import requests

    BiliBili = _bilibili()
    router = _happy_router()
    router["web-interface/nav"] = {"code": -101, "message": "账号未登录", "data": {"isLogin": False}}
    original = requests.session
    _patch_session(requests, router)
    try:
        try:
            BiliBili({"cookie": "SESSDATA=x; bili_jct=y"}).main()
        except RuntimeError as exc:
            assert "登录态失效" in str(exc)
        else:
            raise AssertionError("Cookie 失效时必须抛异常，否则上层会误判为成功")
    finally:
        requests.session = original


def test_main_incomplete_cookie_returns_hint():
    BiliBili = _bilibili()
    out = BiliBili({"cookie": "SESSDATA=x"}).main()
    assert "Cookie 不完整" in out


if __name__ == "__main__":
    failures = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
                print(f"PASS  {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL  {name}: {exc}")
    print(f"\n{'全部通过' if not failures else f'{failures} 个失败'}")
    sys.exit(1 if failures else 0)
