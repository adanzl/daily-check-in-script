"""Microsoft Rewards / Bing 签到模块测试。

不联网, HTTP 全部走 FakeSession。
跑法: `.venv/bin/python tests/test_bing.py`
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from daily_checkin.bing.main import (
    Bing,
    authorization_code,
    claimable_cards,
    clean_cookie,
    edge_plan,
    extract_json_array,
    named_actions,
    streak_state,
)
from daily_checkin.configs import checkin_map

ACTION_ID = "a" * 40


class FakeResp:
    def __init__(self, text="", url="", status_code=200, headers=None):
        self.text = text
        self.url = url
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        raise ValueError("not json")


def _card(marker, title, offer_id, completed, points=10):
    flag = "true" if completed else "false"
    return (
        f'{marker}\\":[{{\\"title\\":\\"{title}\\",\\"points\\":{points},\\"isCompleted\\":{flag},'
        f'\\"offerId\\":\\"{offer_id}\\",\\"hash\\":\\"hash-{offer_id}\\",'
        f'\\"destination\\":\\"https://www.bing.com/search\\"}}]'
    )


class FakeSession:
    def __init__(self, dashboard_status=200, location=""):
        self.posts = []
        self.headers = {}
        self.proxies = {}
        self.dashboard_calls = 0
        self.earn_calls = 0
        self.dashboard_status = dashboard_status
        self.location = location

    def get(self, url, **kwargs):
        if self.dashboard_status != 200 and "dashboard" in url and self.dashboard_calls == 0:
            self.dashboard_calls += 1
            return FakeResp(text="", url=url, status_code=self.dashboard_status, headers={"Location": self.location})
        if "chunks/" in url:
            return FakeResp(text=f'createServerReference("{ACTION_ID}","reportActivity")', url=url)
        if "oauth20_authorize" in url:
            return FakeResp(text="<title>登录</title>", url="https://login.live.com/oauth20_authorize.srf")
        if "dashboard" in url:
            self.dashboard_calls += 1
            completed = self.dashboard_calls > 1
            balance = 110 if completed else 100
            text = (
                '<script src="/_next/static/chunks/app.js"></script>'
                + _card("dailySetItems", "土壤守护者", "offer-1", completed)
                + f'"balance":{balance}'
            )
            return FakeResp(text=text + ("." * 1200), url=url)
        if "earn" in url:
            self.earn_calls += 1
            completed = self.earn_calls > 1
            text = (
                _card("activityCards", "素食汉堡", "offer-2", completed, 15)
                + '"activitiesCompleted":0,"activitiesTotal":30,"partner":"edge"'
                + '"activitiesCompleted":0,"activitiesTotal":1,"partner":"bingapp"'
            )
            return FakeResp(text=text + ("." * 1200), url=url)
        return FakeResp(text="missing", url=url, status_code=404)

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs.get("data"), kwargs.get("headers")))
        return FakeResp(text='0:{"a":"$@1"}\n1:true\n', url=url)


def test_extract_and_select_claimable_cards():
    page = _card("dailySetItems", "土壤守护者", "offer-1", False) + _card("activityCards", "推荐", "offer-3", False, 0)
    daily = extract_json_array(page, "dailySetItems")
    assert daily[0]["title"] == "土壤守护者"
    assert claimable_cards(daily)[0]["offerId"] == "offer-1"
    assert claimable_cards(
        [
            {"offerId": "done", "hash": "h", "points": 5, "isCompleted": True},
            {"offerId": "locked", "hash": "h", "points": 5, "isLocked": True},
            {"offerId": "refer", "hash": "h", "points": 5, "destination": "https://rewards.bing.com/referandearn"},
            {"offerId": "quiz", "hash": "h", "points": 5, "title": "你是否知道答案？"},
        ]
    )[0]["offerId"] == "quiz"


def test_authorization_code_from_redirect_or_raw_value():
    assert authorization_code("https://login.live.com/oauth20_desktop.srf?code=abc.def&state=rewards") == "abc.def"
    assert authorization_code("M.C123-refresh") == ""
    assert authorization_code("raw-code") == "raw-code"


def test_named_actions_and_edge_plan():
    assert named_actions(f'createServerReference("{ACTION_ID}","reportActivity")')["reportActivity"] == ACTION_ID
    assert edge_plan({"complete": "true"}) == (0, 5)
    assert edge_plan({"progress": 0, "max": 30, "report_per_minutes": 5}) == (6, 5)
    page = '"activitiesCompleted":0,"activitiesTotal":30,"partner":"edge"'
    assert streak_state(page, "edge")["total"] == 30


def test_run_claims_daily_and_more_and_reports_app_tasks():
    session = FakeSession()
    message = Bing({"cookie": "a=b"}).run(session)
    assert len(session.posts) == 2
    assert "土壤守护者" in message
    assert "素食汉堡" in message
    assert "本次积分: +10" in message
    assert "可用积分: 110" in message
    assert "每日活动: 1/1 已完成" in message
    assert "更多活动: 1/1 已完成" in message
    assert "Edge 阅读: 0/30" in message
    assert "移动签到: 0/1" in message
    assert "本次领取: 土壤守护者、素食汉堡" in message
    assert "应用访问令牌" in message
    posted = session.posts[0][1].decode()
    assert ACTION_ID == session.posts[0][2]["Next-Action"]
    assert "offer-1" in posted
    assert "offer-2" in session.posts[1][1].decode()


def test_run_login_redirect_raises():
    session = FakeSession(dashboard_status=302, location="https://login.live.com/oauth")
    try:
        Bing({"cookie": "a=b"}).run(session)
    except RuntimeError as exc:
        assert "未登录" in str(exc)
    else:
        raise AssertionError("应当因登录跳转失败")


def test_missing_cookie_raises_before_network():
    try:
        Bing({}).main()
    except RuntimeError as exc:
        assert "cookie" in str(exc)
    else:
        raise AssertionError("应当因缺少 cookie 失败")


def test_proxy_and_cookie_header():
    bot = Bing({"cookie": "Cookie: a=b", "proxy": "http://127.0.0.1:1080"})
    session = bot._build_session(clean_cookie("Cookie: a=b"))
    assert session.headers["Cookie"] == "a=b"
    assert session.proxies["https"] == "http://127.0.0.1:1080"
    ignored = Bing({"proxy": "使用代理的信息"})._build_session("a=b")
    assert ignored.proxies == {}


def test_registered_in_checkin_map():
    name, cls = checkin_map["BING"]
    assert name == "Microsoft Rewards"
    assert cls is Bing


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
