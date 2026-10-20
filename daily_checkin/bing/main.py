import json
import math
import os
import random
import re
import time
import uuid
from urllib.parse import quote, unquote, urljoin

import requests

from daily_checkin import CheckIn

DASHBOARD_URL = "https://rewards.bing.com/dashboard"
EARN_URL = "https://rewards.bing.com/earn"
ORIGIN = "https://rewards.bing.com"
ACTIVITIES_URL = "https://prod.rewardsplatform.microsoft.com/dapi/me/activities"
EDGE_PROFILE_URL = "https://prod.rewardsplatform.microsoft.com/dapi/me?channel=edge&options=Profile,Promotions"
AUTHORIZE_URL = "https://login.live.com/oauth20_authorize.srf"
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
CLIENT_ID = "0000000040170455"
TOKEN_SCOPE = "service::prod.rewardsplatform.microsoft.com::MBI_SSL"
REDIRECT_URI = "https://login.live.com/oauth20_desktop.srf"
# 页面脚本里 reportActivity 的编号。脚本改版时会先从页面里重新找。
KNOWN_REPORT_ACTION = "707e6eb15bdfdd5fba193f0a77e934f7018faf87ce"
APP_CHECKIN_OFFER = "Gamification_Sapphire_DailyCheckIn"
EDGE_PROMOTION = "edge_browsing_streak_flight"
EDGE_OFFER_ID = "DailyCheckIn_Edge"
EDGE_REPORT_TYPE = "29"
EDGE_MINUTES = 30
SEARCH_PAGE = "https://cn.bing.com/search"
SEARCH_REPORT = "https://cn.bing.com/rewardsapp/reportactivity"
FLYOUT_URL = "https://cn.bing.com/rewards/panelflyout/getuserinfo?channel=BingFlyout&partnerId=BingRewards"
HOT_BOARD_URL = "https://top.baidu.com/api/board?platform=wise&tab=realtime"
PAGE_LANGUAGES = ("zh-CN,zh;q=0.9,en;q=0.8", "en-US,en;q=0.9")
TIMEOUT = 25
LOGIN_HOSTS = {
    "login.live.com",
    "login.microsoftonline.com",
    "login.windows.net",
    "account.live.com",
}
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:158.0) Gecko/20100101 Firefox/158.0"
APP_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36 BingSapphire/32.6.2110003560"
)
ACTION_PATTERN = re.compile(
    r'createServerReference\s*\)?\s*\(\s*"([a-f0-9]{40,64})"([\s\S]{0,800}?)\)'
)
IGNORED_ACTION_NAMES = {"callServer", "findSourceMapURL", "encodeFormAction", "default"}
ACK_PATTERN = re.compile(r"^\d+:true\s*$", re.M)


def clean_cookie(raw):
    cookie = str(raw or "").strip().strip('"').strip("'")
    if cookie.lower().startswith("cookie:"):
        cookie = cookie.split(":", 1)[1].strip()
    return cookie.replace("\r", "").replace("\n", "")


def as_int(raw):
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
        return int(raw.strip())
    return None


def timezone_offset():
    seconds = time.altzone if time.localtime().tm_isdst and time.daylight else time.timezone
    return str(-seconds // 60)


def is_login_url(url):
    host = (url or "").split("/")[2].split(":")[0].lower() if "://" in (url or "") else ""
    if not host and url and url.startswith("/"):
        return "signin" in url.lower()
    return host in LOGIN_HOSTS or host.endswith(".login.live.com") or "signin" in (url or "").lower()


def js_unescape(raw):
    text = raw or ""
    out = []
    index = 0
    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text):
            nxt = text[index + 1]
            if nxt == "u" and index + 5 < len(text) and re.fullmatch(r"[0-9a-fA-F]{4}", text[index + 2 : index + 6]):
                out.append(chr(int(text[index + 2 : index + 6], 16)))
                index += 6
                continue
            out.append({"\\": "\\", '"': '"', "/": "/", "n": "\n", "r": "\r", "t": "\t"}.get(nxt, nxt))
            index += 2
            continue
        out.append(text[index])
        index += 1
    return "".join(out)


def match_brackets(text, start):
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def extract_json_array(text, marker):
    index = (text or "").find(marker)
    if index < 0:
        return []
    window = js_unescape(text[index : index + 120000]).replace("$undefined", "null")
    start = window.find("[")
    if start < 0:
        return []
    blob = match_brackets(window, start)
    if not blob:
        return []
    try:
        value = json.loads(blob)
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def latest_number(text, key):
    found = re.findall(rf'"{key}"\s*:\s*(\d+)', js_unescape(text or ""))
    return int(found[-1]) if found else None


def is_future_offer(item):
    offer_id = str((item or {}).get("offerId") or "")
    match = re.search(r"(20\d{6})", offer_id)
    return bool(match and match.group(1) > time.strftime("%Y%m%d"))


def is_claimable(item):
    if not isinstance(item, dict):
        return False
    if item.get("isCompleted") is True or item.get("complete") is True:
        return False
    if item.get("isLocked") is True or is_future_offer(item):
        return False
    points = as_int(item.get("points"))
    offer_id = str(item.get("offerId") or "").strip()
    digest = str(item.get("hash") or "").strip()
    destination = str(item.get("destination") or "").lower()
    if points is None or points <= 0 or not offer_id or not digest or digest == "null":
        return False
    return "referandearn" not in destination


def claimable_cards(items):
    found = []
    seen = set()
    for item in items or []:
        if not is_claimable(item):
            continue
        offer_id = str(item.get("offerId"))
        if offer_id in seen:
            continue
        seen.add(offer_id)
        found.append(item)
    return found


def card_title(item):
    title = str(item.get("title") or item.get("name") or "活动").strip()
    return title or "活动"


def unique_cards(items):
    found = []
    seen = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        offer_id = str(item.get("offerId") or "")
        if offer_id and offer_id in seen:
            continue
        if offer_id:
            seen.add(offer_id)
        found.append(item)
    return found


def parse_hot_queries(payload):
    words = []
    data = payload.get("data") if isinstance(payload, dict) else None
    cards = data.get("cards") if isinstance(data, dict) else None
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        for block in card.get("content") or []:
            items = block.get("content") if isinstance(block, dict) else None
            if isinstance(block, dict) and block.get("word"):
                items = [block]
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                word = str(item.get("word") or item.get("query") or "").strip()
                if len(word) < 2 or word in words:
                    continue
                words.append(word)
    return words


def pc_search_quota(promotions):
    found = None
    step = 3
    for promo in promotions or []:
        if not isinstance(promo, dict):
            continue
        attrs = promo.get("attributes") if isinstance(promo.get("attributes"), dict) else {}
        if promo.get("name") == "level_info":
            parsed = as_int(attrs.get("points_per_pc_search"))
            if parsed and parsed > 0:
                step = parsed
        if str(attrs.get("type") or "") != "search":
            continue
        name = str(promo.get("name") or "")
        if "PC" not in name and attrs.get("AnswerScenario.Tag") != "PCSearch":
            continue
        progress = as_int(attrs.get("progress"))
        maximum = as_int(attrs.get("max"))
        if progress is None or not maximum or maximum <= 0:
            continue
        found = (max(0, progress), maximum)
    if not found:
        return None
    return {"done": found[0], "total": found[1], "step": step}


def named_actions(script):
    found = {}
    for match in ACTION_PATTERN.finditer(script or ""):
        names = [
            name
            for name in re.findall(r'"([A-Za-z_$][\w$]*)"', match.group(2))
            if name not in IGNORED_ACTION_NAMES and len(name) > 3
        ]
        if names:
            found[names[-1]] = match.group(1)
    return found


def router_state_tree(segment):
    tree = [
        "",
        {
            "children": [
                "(nav)",
                {
                    "children": [
                        segment,
                        {"children": ["PAGE", {}, None, None, 4096]},
                        None,
                        None,
                        4096,
                    ]
                },
                None,
                None,
                4096,
            ]
        },
        None,
        None,
        4112,
    ]
    return quote(json.dumps(tree, separators=(",", ":")))


def streak_state(text, partner):
    source = js_unescape(text or "")
    index = 0
    while True:
        found = source.find(f'"partner":"{partner}"', index)
        if found < 0:
            return None
        window = source[max(0, found - 1800) : found]
        done = re.findall(r'"activitiesCompleted":(\d+)', window)
        total = re.findall(r'"activitiesTotal":(\d+)', window)
        if done and total:
            offer = re.findall(r'"activationOfferId":"([^"]+)"', window)
            activity = re.findall(r'"activationActivityType":(\d+)', window)
            offer_id = offer[-1] if offer and offer[-1] != "$undefined" else ""
            return {
                "done": int(done[-1]),
                "total": int(total[-1]),
                "offer_id": offer_id,
                "activity_type": int(activity[-1]) if activity else None,
            }
        index = found + 10


def authorization_code(raw):
    text = str(raw or "").strip()
    matched = re.search(r"[?&]code=([^&\s#]+)", text)
    if matched:
        return unquote(matched.group(1))
    if "://" in text or text.lower().startswith("m."):
        return ""
    return text


def activity_points(payload):
    if not isinstance(payload, dict):
        return None, "返回不是 JSON"
    response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
    if response.get("isDuplicate") is True:
        return 0, "今日已完成"
    activity = response.get("activity") if isinstance(response.get("activity"), dict) else {}
    points = as_int(activity.get("p"))
    if points is None:
        points = as_int(activity.get("points"))
    if points is not None and points > 0:
        return points, "获得积分"
    if points == 0 or payload.get("code") == 0:
        return 0, "今日已完成" if points == 0 else "已提交"
    return None, "未确认结果"


def edge_plan(attributes):
    attrs = attributes if isinstance(attributes, dict) else {}
    if str(attrs.get("complete") or "").lower() == "true":
        return 0, 5
    interval = as_int(attrs.get("report_per_minutes")) or 5
    interval = min(30, max(1, interval))
    progress = as_int(attrs.get("progress")) or 0
    maximum = as_int(attrs.get("max")) or EDGE_MINUTES
    # 接口里的 max 可能是整段活动上限。页面上的每日目标是 30 分钟。
    if maximum <= 0 or maximum > EDGE_MINUTES:
        maximum = EDGE_MINUTES
    if progress < 0 or progress > maximum:
        progress = 0
    remaining = max(0, maximum - progress)
    count = math.ceil(remaining / interval) if remaining else 0
    return min(8, count), interval


class Bing(CheckIn):
    name = "Microsoft Rewards"

    def __init__(self, check_item):
        self.check_item = check_item

    def _build_session(self, cookie):
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Cookie": cookie,
            }
        )
        proxy = str(self.check_item.get("proxy") or "").strip()
        if "://" in proxy:
            session.proxies.update({"http": proxy, "https": proxy})
        return session

    def _get_page(self, session, url, language=None):
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": DASHBOARD_URL,
        }
        if language:
            headers["Accept-Language"] = language
        last_error = "请求失败"
        current = url
        for _ in range(3):
            try:
                response = session.get(current, headers=headers, timeout=TIMEOUT, allow_redirects=False)
            except requests.RequestException as exc:
                last_error = f"请求失败: {exc.__class__.__name__}"
                continue
            status = getattr(response, "status_code", 200)
            location = ""
            response_headers = getattr(response, "headers", {}) or {}
            if hasattr(response_headers, "get"):
                location = response_headers.get("Location") or response_headers.get("location") or ""
            if status in (301, 302, 303, 307, 308):
                if is_login_url(location):
                    raise RuntimeError("Cookie 失效或未登录 Microsoft Rewards")
                current = urljoin(current, location)
                continue
            if status == 200 and len(getattr(response, "text", "") or "") > 1000:
                return response.text or ""
            last_error = f"请求失败: HTTP {status}"
        raise RuntimeError(last_error)

    def _discover_action(self, session, html):
        for src in re.findall(r'src="(/_next/static/chunks/[^"]+\.js[^"]*)"', html or ""):
            try:
                response = session.get(urljoin(ORIGIN, src), timeout=TIMEOUT, allow_redirects=False)
            except requests.RequestException:
                continue
            actions = named_actions(getattr(response, "text", "") or "")
            if actions.get("reportActivity"):
                return actions["reportActivity"]
        return KNOWN_REPORT_ACTION

    def _boards(self, session):
        daily, more = [], []
        dashboard = earn = ""
        for language in PAGE_LANGUAGES:
            dashboard = self._get_page(session, DASHBOARD_URL, language)
            earn = self._get_page(session, EARN_URL, language)
            daily.extend(extract_json_array(dashboard, "dailySetItems"))
            more.extend(extract_json_array(earn, "activityCards"))
        return unique_cards(daily), unique_cards(more), dashboard, earn

    def _flyout_promotions(self, session):
        try:
            response = session.get(
                FLYOUT_URL,
                headers={"Accept": "application/json", "Referer": "https://cn.bing.com/"},
                timeout=TIMEOUT,
            )
            payload = response.json()
        except (requests.RequestException, ValueError):
            return None
        user = payload.get("userInfo") if isinstance(payload, dict) else None
        promotions = user.get("promotions") if isinstance(user, dict) else None
        return promotions if isinstance(promotions, list) else None

    def _hot_queries(self, session):
        bare = self._token_session(session)
        bare.headers["Accept-Language"] = "zh-CN,zh;q=0.9"
        try:
            response = bare.get(
                HOT_BOARD_URL,
                headers={"Accept": "application/json", "Referer": "https://top.baidu.com/board?tab=realtime"},
                timeout=TIMEOUT,
            )
            payload = response.json()
        except (requests.RequestException, ValueError):
            return []
        words = parse_hot_queries(payload)
        random.shuffle(words)
        return words

    def _one_search(self, session, query):
        try:
            page = session.get(
                SEARCH_PAGE,
                params={"q": query, "form": "QBLH"},
                headers={"Referer": "https://cn.bing.com/"},
                timeout=TIMEOUT,
            )
        except requests.RequestException:
            return False
        text = getattr(page, "text", "") or ""
        ig = re.search(r'IG:"([A-F0-9]+)"', text)
        if not ig:
            return True
        iid = re.search(r'data-iid="([^"]+)"', text, re.I)
        report_url = f"{SEARCH_REPORT}?IG={ig.group(1)}&IID={iid.group(1) if iid else 'SERP.5000'}&q={quote(query)}"
        body = "url=" + quote(getattr(page, "url", "") or "", safe="") + "&V=web"
        try:
            session.post(
                report_url,
                data=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": getattr(page, "url", SEARCH_PAGE),
                    "Origin": "https://cn.bing.com",
                },
                timeout=TIMEOUT,
            )
        except requests.RequestException:
            return False
        return True

    def _report_searches(self, session):
        quota = pc_search_quota(self._flyout_promotions(session))
        if quota is None:
            return "电脑搜索: 暂时读不到进度"
        if quota["done"] >= quota["total"]:
            return f"电脑搜索: {quota['done']}/{quota['total']} 今日已完成"
        words = self._hot_queries(session)
        if not words:
            return f"电脑搜索: {quota['done']}/{quota['total']}，没有取到热词"
        needed = math.ceil(max(0, quota["total"] - quota["done"]) / quota["step"])
        limit = min(len(words), needed + 2, 15)
        for index, query in enumerate(words[:limit], start=1):
            if quota["done"] >= quota["total"]:
                break
            print(f"电脑搜索 {index}/{limit}: {query}")
            self._one_search(session, query)
            refreshed = pc_search_quota(self._flyout_promotions(session))
            if refreshed:
                quota = refreshed
            print(f"电脑搜索进度 {quota['done']}/{quota['total']}")
            if quota["done"] >= quota["total"]:
                return f"电脑搜索: {quota['done']}/{quota['total']} 今日已完成"
            if index < limit:
                time.sleep(random.randint(8, 14))
        return f"电脑搜索: {quota['done']}/{quota['total']}"

    def _claim(self, session, item, page, action_id):
        body = [
            item.get("hash"),
            11,
            {
                "offerid": item.get("offerId"),
                "isPromotional": "$undefined",
                "timezoneOffset": timezone_offset(),
            },
        ]
        try:
            response = session.post(
                f"{ORIGIN}/{page}",
                data=json.dumps(body, ensure_ascii=False).encode(),
                headers={
                    "Accept": "text/x-component",
                    "Content-Type": "text/plain;charset=UTF-8",
                    "Next-Action": action_id,
                    "Next-Router-State-Tree": router_state_tree(page),
                    "Origin": ORIGIN,
                    "Referer": f"{ORIGIN}/{page}",
                },
                timeout=TIMEOUT,
                allow_redirects=False,
            )
        except requests.RequestException:
            return False
        return ACK_PATTERN.search(getattr(response, "text", "") or "") is not None

    def _token_session(self, session):
        bare = requests.Session()
        bare.headers["User-Agent"] = USER_AGENT
        if getattr(session, "proxies", None):
            bare.proxies.update(session.proxies)
        return bare

    def _access_token(self, session):
        token_session = self._token_session(session)
        refresh = str(self.check_item.get("refresh_token") or "").strip()
        if refresh:
            token, rotated = self._token_from_refresh(token_session, refresh)
            if rotated:
                self._save_refresh_token(rotated)
            if token:
                return token
        code = authorization_code(self.check_item.get("code"))
        if not code:
            params = {
                "client_id": CLIENT_ID,
                "response_type": "code",
                "redirect_uri": REDIRECT_URI,
                "scope": TOKEN_SCOPE,
                "state": "rewards",
            }
            try:
                response = session.get(AUTHORIZE_URL, params=params, timeout=TIMEOUT, allow_redirects=True)
            except requests.RequestException:
                return ""
            code = authorization_code(getattr(response, "url", "") or "")
        if not code:
            return ""
        token, refresh = self._exchange_code(token_session, code)
        if refresh:
            self._save_refresh_token(refresh)
        return token

    def _save_refresh_token(self, refresh):
        cookie = clean_cookie(self.check_item.get("cookie"))
        for relative in ("config.json", "../config.json", "./config/config.json", "../config/config.json"):
            path = os.path.normpath(os.path.join(os.getcwd(), relative))
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
                accounts = data.get("BING")
                if not isinstance(accounts, list):
                    continue
                updated = False
                for item in accounts:
                    if isinstance(item, dict) and clean_cookie(item.get("cookie")) == cookie:
                        item["refresh_token"] = refresh
                        item.pop("code", None)
                        updated = True
                if not updated:
                    continue
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
            except (OSError, json.JSONDecodeError):
                continue
            self.check_item["refresh_token"] = refresh
            self.check_item.pop("code", None)
            return

    @staticmethod
    def _exchange_code(session, code):
        form = {
            "client_id": CLIENT_ID,
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
            "scope": TOKEN_SCOPE,
        }
        for url in (TOKEN_URL, "https://login.live.com/oauth20_token.srf"):
            try:
                response = session.post(url, data=form, timeout=TIMEOUT)
                payload = response.json()
            except (requests.RequestException, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            token = payload.get("access_token")
            refresh = payload.get("refresh_token")
            if isinstance(token, str) and token:
                return token, refresh if isinstance(refresh, str) else ""
        return "", ""

    @staticmethod
    def _token_from_refresh(session, refresh):
        try:
            response = session.post(
                TOKEN_URL,
                data={
                    "client_id": CLIENT_ID,
                    "refresh_token": refresh,
                    "grant_type": "refresh_token",
                    "scope": TOKEN_SCOPE,
                },
                timeout=TIMEOUT,
            )
            payload = response.json()
        except (requests.RequestException, ValueError):
            return "", ""
        if not isinstance(payload, dict):
            return "", ""
        token = payload.get("access_token")
        rotated = payload.get("refresh_token")
        return (token if isinstance(token, str) else ""), (rotated if isinstance(rotated, str) else "")

    def _post_activity(self, session, token, payload, mobile):
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "User-Agent": APP_USER_AGENT if mobile else USER_AGENT,
        }
        if mobile:
            headers.update(
                {
                    "X-Rewards-AppId": "SAAndroid/32.6.2110003560",
                    "X-Rewards-PartnerId": "startapp",
                    "X-Rewards-IsMobile": "true",
                    "X-Rewards-Flights": "rwgobig",
                    "X-Rewards-Country": payload.get("country") or "cn",
                    "X-Rewards-Language": "zh",
                }
            )
        else:
            headers.update(
                {
                    "X-Rewards-AppId": "EdgeDesktop",
                    "X-Rewards-PartnerId": "EdgeHub",
                    "X-Rewards-Country": payload.get("country") or "cn",
                    "X-Rewards-Language": "zh-CN",
                }
            )
        try:
            response = session.post(ACTIVITIES_URL, json=payload, headers=headers, timeout=TIMEOUT)
            data = response.json()
        except (requests.RequestException, ValueError):
            return None, "请求失败"
        return activity_points(data)

    def _edge_profile(self, session, token):
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "X-Rewards-AppId": "EdgeDesktop",
            "X-Rewards-PartnerId": "EdgeHub",
            "X-Rewards-Country": "cn",
            "X-Rewards-Language": "zh-CN",
        }
        for _ in range(3):
            try:
                response = session.get(EDGE_PROFILE_URL, headers=headers, timeout=TIMEOUT)
                payload = response.json()
            except (requests.RequestException, ValueError):
                continue
            body = payload.get("response") if isinstance(payload, dict) else None
            promotions = body.get("promotions") if isinstance(body, dict) else None
            if not isinstance(promotions, list):
                continue
            for item in promotions:
                if isinstance(item, dict) and item.get("name") == EDGE_PROMOTION:
                    return item
        return None

    def _report_edge(self, session, token, country):
        profile = self._edge_profile(session, token)
        attributes = profile.get("attributes") if isinstance(profile, dict) else {}
        if not isinstance(attributes, dict):
            attributes = {}
        offer_id = str(attributes.get("offerid") or EDGE_OFFER_ID).strip()
        activity_type = str(attributes.get("report_offer_type") or EDGE_REPORT_TYPE).strip()
        if not attributes:
            attributes = {"report_per_minutes": "5", "progress": "0", "max": str(EDGE_MINUTES)}
        _, interval = edge_plan(attributes)
        current = self._edge_page_progress(session)
        if current and current["total"] > 0 and current["done"] >= current["total"]:
            return f"Edge 阅读: {current['done']}/{current['total']} 今日已完成"
        if current and current["total"] > 0:
            remaining = max(0, current["total"] - current["done"])
            count = math.ceil(remaining / interval) if remaining else 0
        else:
            count, interval = edge_plan(attributes)
        if count <= 0:
            return "Edge 阅读: 今日已完成"
        accepted = 0
        for index in range(count):
            points, state = self._post_activity(
                session,
                token,
                {
                    "amount": 1,
                    "attributes": {"offerid": offer_id},
                    "request_user_info": True,
                    "type": activity_type,
                    "country": country,
                },
                mobile=False,
            )
            if points is None:
                for _ in range(2):
                    time.sleep(15)
                    points, state = self._post_activity(
                        session,
                        token,
                        {
                            "amount": 1,
                            "attributes": {"offerid": offer_id},
                            "request_user_info": True,
                            "type": activity_type,
                            "country": country,
                        },
                        mobile=False,
                    )
                    if points is not None:
                        break
            if points is None:
                current = self._edge_page_progress(session)
                if current and current["total"] > 0:
                    return f"Edge 阅读: {current['done']}/{current['total']}，上报中断，{state}"
                return f"Edge 阅读: 上报失败，{state}"
            accepted += 1
            current = self._edge_page_progress(session)
            done_text = f"{current['done']}/{current['total']}" if current else "已上报"
            print(f"Edge 阅读上报 {index + 1}/{count}，进度 {done_text}")
            if current and current["total"] > 0 and current["done"] >= current["total"]:
                return f"Edge 阅读: {current['done']}/{current['total']} 今日已完成"
            if index + 1 < count:
                time.sleep(interval * 60)
        if current and current["total"] > 0:
            return f"Edge 阅读: {current['done']}/{current['total']}"
        if accepted:
            return f"Edge 阅读: 已上报 {accepted} 次"
        return "Edge 阅读: 未完成"

    def _edge_page_progress(self, session):
        try:
            page = self._get_page(session, EARN_URL)
        except RuntimeError:
            return None
        return streak_state(page, "edge")

    def _report_mobile(self, session, token, country):
        points, state = self._post_activity(
            session,
            token,
            {
                "amount": 1,
                "id": str(uuid.uuid4()),
                "type": 103,
                "country": country,
                "channel": "SAAndroid",
                "attributes": {"offerid": APP_CHECKIN_OFFER},
            },
            mobile=True,
        )
        if points is None:
            return f"移动签到: {state}"
        if points > 0:
            return f"移动签到: 成功，获得 {points} 积分"
        return f"移动签到: {state}"

    def _summarize_cards(self, label, items):
        scored = [
            item
            for item in items or []
            if (as_int(item.get("points")) or 0) > 0
            and "referandearn" not in str(item.get("destination") or "").lower()
        ]
        if not scored:
            return f"{label}: 没有可做的活动"
        done = [item for item in scored if item.get("isCompleted") is True or item.get("complete") is True]
        pending = claimable_cards(scored)
        if pending:
            names = "、".join(card_title(item) for item in pending)
            return f"{label}: {len(done)}/{len(done) + len(pending)}，未完成 {names}"
        return f"{label}: {len(done)}/{len(done)} 已完成"

    def run(self, session):
        daily_cards, more_cards, dashboard, earn = self._boards(session)
        if "dailySetItems" not in dashboard and "activityCards" not in earn:
            raise RuntimeError("Cookie 失效或未登录 Microsoft Rewards")
        before = latest_number(dashboard, "balance")
        if before is None:
            before = latest_number(earn, "balance")
        action_id = self._discover_action(session, dashboard + earn)
        daily = claimable_cards(daily_cards)
        more = claimable_cards(more_cards)
        claimed = []
        for item in daily:
            if self._claim(session, item, "dashboard", action_id):
                claimed.append(card_title(item))
        for item in more:
            if self._claim(session, item, "earn", action_id):
                claimed.append(card_title(item))
        daily_cards, more_cards, dashboard, earn = self._boards(session)
        after = latest_number(dashboard, "balance")
        if after is None:
            after = latest_number(earn, "balance")
        lines = [
            self._summarize_cards("每日活动", daily_cards),
            self._summarize_cards("更多活动", more_cards),
            self._report_searches(session),
        ]
        if claimed:
            lines.append("本次领取: " + "、".join(claimed))
        if after is not None:
            if before is not None and after > before:
                lines.append(f"本次积分: +{after - before}")
            lines.append(f"可用积分: {after}")
        edge = streak_state(earn, "edge") or streak_state(dashboard, "edge")
        mobile = streak_state(earn, "bingapp") or streak_state(dashboard, "bingapp")
        token = self._access_token(session)
        country = "cn"
        if token:
            lines.append(self._report_edge(session, token, country))
            if mobile and mobile["total"] and mobile["done"] >= mobile["total"]:
                lines.append(f"移动签到: {mobile['done']}/{mobile['total']} 今日已完成")
            else:
                lines.append(self._report_mobile(session, token, country))
        else:
            edge_text = f"{edge['done']}/{edge['total']}" if edge else "未读取"
            mobile_text = f"{mobile['done']}/{mobile['total']}" if mobile else "未读取"
            lines.append(f"Edge 阅读: {edge_text}")
            lines.append(f"移动签到: {mobile_text}")
            lines.append("Edge 阅读和移动签到需要 Rewards 应用访问令牌，当前网页 Cookie 换不到")
        pending = claimable_cards(daily_cards) + claimable_cards(more_cards)
        if pending and not claimed:
            raise RuntimeError("\n".join(lines + ["签到失败: 活动没有领到"]))
        return "\n".join(lines)

    def main(self):
        cookie = clean_cookie(self.check_item.get("cookie"))
        if not cookie:
            raise RuntimeError("未配置 cookie")
        return self.run(self._build_session(cookie))


if __name__ == "__main__":
    with open(
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json"),
        encoding="utf-8",
    ) as f:
        data_str = json.loads(f.read())
    _check_item = data_str.get("BING", [])[0]
    print(Bing(check_item=_check_item).main())
