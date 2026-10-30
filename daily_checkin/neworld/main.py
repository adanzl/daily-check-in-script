import hashlib
import json
import os
import re

import requests

from daily_checkin import CheckIn

_BASE = "https://neworld.cloud"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_TRAFFIC_RE = re.compile(r"剩余流量\s*([0-9]+(?:\.[0-9]+)?\s*[KMGT]?B)", re.IGNORECASE)


def fingerprint_for(email):
    """登录页会带浏览器指纹。同一邮箱固定一个值，避免每次都被当成新设备。"""
    return hashlib.sha256(email.encode()).hexdigest()[:32]


def parse_remaining_traffic(html):
    match = _TRAFFIC_RE.search(html or "")
    if not match:
        return ""
    return re.sub(r"\s+", "", match.group(1))


def _as_json(response):
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"接口返回不是 JSON，状态码 {response.status_code}") from exc


class Neworld(CheckIn):
    name = "新界"

    def __init__(self, check_item):
        self.check_item = check_item

    def _session(self):
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": _USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Origin": _BASE,
                "Referer": f"{_BASE}/auth/login",
            }
        )
        return session

    def login(self, session, email, password, totp):
        response = session.post(
            f"{_BASE}/auth/login",
            json={
                "code": totp,
                "email": email,
                "passwd": password,
                "remember_me": "false",
                "fingerprint": fingerprint_for(email),
            },
            timeout=30,
        )
        response.raise_for_status()
        data = _as_json(response)
        if data.get("ret") != 1:
            raise RuntimeError(data.get("msg") or "登录失败")
        return data.get("msg") or "登录成功"

    def checkin(self, session, checkin_type):
        response = session.post(
            f"{_BASE}/user/checkin",
            data={"checkin_type": checkin_type},
            headers={"Referer": f"{_BASE}/user"},
            timeout=30,
        )
        response.raise_for_status()
        data = _as_json(response)
        message = str(data.get("msg") or "").strip() or "签到失败"
        # ret 2 表示站点要求先过人机验证，不带验证结果时不要当成已签到
        if data.get("ret") == 2:
            raise RuntimeError("签到需要人机验证，这次没有领到")
        if data.get("ret") == 1 or "已签到" in message or "已经签到" in message:
            traffic = str(data.get("traffic") or "").strip()
            return message, traffic
        raise RuntimeError(message)

    def remaining_traffic(self, session):
        response = session.get(f"{_BASE}/user", timeout=30)
        response.raise_for_status()
        return parse_remaining_traffic(response.text)

    def main(self):
        email = str(self.check_item.get("email") or "").strip()
        password = str(self.check_item.get("password") or "")
        totp = str(self.check_item.get("totp") or "").strip()
        if not email or not password:
            raise RuntimeError("未配置邮箱或密码")
        checkin_type = str(self.check_item.get("checkin_type") or "traffic").strip().lower()
        if checkin_type not in {"traffic", "time"}:
            raise RuntimeError("签到类型只能是 traffic 或 time")
        session = self._session()
        login_message = self.login(session, email, password, totp)
        check_message, traffic = self.checkin(session, checkin_type)
        reward = "流量" if checkin_type == "traffic" else "时长"
        if not traffic:
            traffic = self.remaining_traffic(session)
        lines = [
            f"账号: {email}",
            f"登录: {login_message}",
            f"签到: {check_message}（{reward}）",
        ]
        if traffic:
            lines.append(f"剩余流量: {traffic}")
        return "\n".join(lines)


if __name__ == "__main__":
    with open(
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json"),
        encoding="utf-8",
    ) as f:
        datas = json.loads(f.read())
    _check_item = datas.get("NEWORLD", [])[0]
    print(Neworld(check_item=_check_item).main())
