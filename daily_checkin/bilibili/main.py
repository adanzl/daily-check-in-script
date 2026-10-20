import json
import os
import time

import requests

from daily_checkin import CheckIn


class BiliBili(CheckIn):
    name = "Bilibili"

    def __init__(self, check_item: dict):
        self.check_item = check_item

    @staticmethod
    def get_nav(session):
        url = "https://api.bilibili.com/x/web-interface/nav"
        try:
            ret = session.get(url=url, timeout=15).json()
        except Exception:
            ret = {}
        # `data` 存在但值是 null 时，`.get("data", {})` 的默认值不生效，
        # 后面的 `.get(...)` 会落到 None 上 —— 必须用 `or {}`。
        data = ret.get("data") or {}
        level_info = data.get("level_info") or {}
        uname = data.get("uname")
        uid = data.get("mid")
        is_login = data.get("isLogin")
        coin = data.get("money")
        vip_type = data.get("vipType")
        current_exp = level_info.get("current_exp")
        return uname, uid, is_login, coin, vip_type, current_exp

    @staticmethod
    def _exp_log_list(session: requests.Session) -> list:
        """取今日经验明细。

        原实现写的是 `.get("data").get("list")` —— 接口返回 `data: null` 时
        `.get("list")` 会落到 None 上抛 AttributeError。这里统一兜成空列表。
        """
        url = "https://api.bilibili.com/x/member/web/exp/log?jsonp=jsonp"
        today = time.strftime("%Y-%m-%d", time.localtime())
        try:
            ret = session.get(url=url, timeout=15).json()
        except Exception:
            return []
        raw = (ret.get("data") or {}).get("list") or []
        if not isinstance(raw, list):
            return []
        return [one for one in raw if isinstance(one, dict) and str(one.get("time", "")).split()[0:1] == [today]]

    @staticmethod
    def get_today_exp(session: requests.Session) -> list:
        """GET 获取今日经验信息
        :param requests.Session session:
        :return list: 今日经验信息列表
        """
        return BiliBili._exp_log_list(session)

    @staticmethod
    def vip_privilege_my(session) -> dict:
        """取B站大会员硬币经验信息（非 JSON 响应不抛异常）"""
        url = "https://api.bilibili.com/x/vip/privilege/my"
        resp = None
        try:
            resp = session.get(url=url, timeout=15)
            return resp.json()
        except Exception as exc:
            return {
                "code": getattr(resp, "status_code", -1),
                "message": f"非JSON响应或请求失败({type(exc).__name__})",
            }

    @staticmethod
    def reward(session) -> dict:
        """取B站经验信息（今日明细，与 get_today_exp 同源）"""
        return BiliBili._exp_log_list(session)

    @staticmethod
    def live_sign(session) -> dict:
        """B站直播签到

        B站已经下线直播签到活动（DoSign 恒返回 `code=1 / 签到活动已下线，无法使用`），
        这不是脚本的问题，也不该显示成「签到失败」。这里单独标出来。
        """
        # 活动下线 / 不可用的返回特征
        offline_markers = ("已下线", "无法使用", "活动已结束")
        try:
            url = "https://api.live.bilibili.com/xlive/web-ucenter/v1/sign/DoSign"
            ret = session.get(url=url, timeout=15).json()
            if not isinstance(ret, dict):
                msg = f"签到异常，原因为: 响应不是 JSON 对象({type(ret).__name__})"
                print(msg)
                return msg
            code = ret.get("code")
            text = str(ret.get("message") or ret.get("msg") or "")
            if code == 0:
                data = ret.get("data") or {}
                msg = (
                    f"签到成功，{data.get('text')}，特别信息:{data.get('specialText')}，"
                    f"本月已签到{data.get('hadSignDays')}天"
                )
            elif code == 1011040 or "已签到" in text:
                msg = "今日已签到过,无法重复签到"
            elif any(marker in text for marker in offline_markers):
                # 平台下线该活动，不是失败
                msg = f"活动已下线，无需签到({text})"
            else:
                msg = f"签到失败，信息为: {text}"
        except Exception as e:
            msg = f"签到异常，原因为{e!s}"
            print(msg)
        return msg

    @staticmethod
    def manga_sign(session, platform="android") -> dict:
        """
        模拟B站漫画客户端签到

        注意「今天已经签过了」的判定：原实现只认英文 `clockin clockin is duplicate`，
        但 B站现在返回中文 `不能重复签到~`，于是把「已签到」当成了「签到失败」。
        这里把两种情况都归成已签到。
        """
        # 表示「今天已经签过」的返回特征，中英文都覆盖
        duplicate_markers = ("duplicate", "不能重复签到", "已经签到", "已签到")
        try:
            url = "https://manga.bilibili.com/twirp/activity.v1.Activity/ClockIn"
            post_data = {"platform": platform}
            ret = session.post(url=url, data=post_data, timeout=15).json()
            if not isinstance(ret, dict):
                msg = f"签到异常,原因为: 响应不是 JSON 对象({type(ret).__name__})"
                print(msg)
                return msg
            code = ret.get("code")
            text = str(ret.get("msg") or ret.get("message") or "")
            if code == 0:
                msg = "签到成功"
            elif any(marker in text for marker in duplicate_markers):
                msg = f"今天已经签到过了({text})" if text else "今天已经签到过了"
            else:
                msg = f"签到失败，信息为({text})"
                print(msg)
        except Exception as e:
            msg = f"签到异常,原因为: {e!s}"
            print(msg)
        return msg

    @staticmethod
    def vip_privilege_receive(session, bili_jct, receive_type: int = 1) -> dict:
        """
        领取B站大会员权益

        该接口在触发风控时返回 **HTTP 412 + HTML**，原实现直接 `.json()` 会抛
        `JSONDecodeError`，把整个签到带崩。这里改成：非 JSON 响应不抛异常，
        归一成 `{"code": <状态码>, "message": ...}`，让调用方按原有
        「code != 0 就当失败」的逻辑走。

        receive_type int 权益类型 1为B币劵 2为优惠券
        """
        url = "https://api.bilibili.com/x/vip/privilege/receive"
        post_data = {"type": receive_type, "csrf": bili_jct}
        resp = None
        try:
            resp = session.post(url=url, data=post_data, timeout=15)
            return resp.json()
        except Exception as exc:
            return {
                "code": getattr(resp, "status_code", -1),
                "message": f"非JSON响应或请求失败({type(exc).__name__})",
            }

    @staticmethod
    def vip_manga_reward(session) -> dict:
        """获取漫画大会员福利"""
        url = "https://manga.bilibili.com/twirp/user.v1.User/GetVipReward"
        ret = session.post(url=url, json={"reason_id": 1}).json()
        return ret

    @staticmethod
    def report_task(session, bili_jct, aid: int, cid: int, progres: int = 300, enabled: bool = False) -> dict:
        """
        B站上报视频观看进度（即「观看视频 300 秒」任务）

        注意：该接口会往「观看历史」里写一条记录。经验在 Lv6 封顶后这 5 点经验
        没有收益，却会污染观看历史（连手机端「继续观看」都会受影响），所以默认
        关闭。要开启请在 config.json 里设 `"watch_video": true`。

        aid int 视频av号
        cid int 视频cid号
        progres int 观看秒数
        enabled bool 是否真的上报，由配置 BILIBILI.watch_video 控制
        """
        if not enabled:
            return {"code": -1, "message": "已跳过（未上报，不污染观看历史）", "skipped": True}
        url = "http://api.bilibili.com/x/v2/history/report"
        post_data = {"aid": aid, "cid": cid, "progres": progres, "csrf": bili_jct}
        try:
            ret = session.post(url=url, data=post_data, timeout=15).json()
        except Exception as exc:
            return {"code": -1, "message": f"非JSON响应或请求失败({type(exc).__name__})"}
        return ret if isinstance(ret, dict) else {"code": -1, "message": "响应不是 JSON 对象"}

    @staticmethod
    def share_task(session, bili_jct, aid) -> dict:
        """
        分享指定av号视频

        B站对「当天已经分享过」返回 code=71000（重复分享）。这本来就是想要的结果，
        所以归一成 code=0，避免被判成任务失败。

        aid int 视频av号
        """
        url = "https://api.bilibili.com/x/web-interface/share/add"
        post_data = {"aid": aid, "csrf": bili_jct}
        try:
            ret = session.post(url=url, data=post_data, timeout=15).json()
        except Exception as exc:
            return {"code": -1, "message": f"非JSON响应或请求失败({type(exc).__name__})"}
        if isinstance(ret, dict) and ret.get("code") == 71000:
            # 今天已经分享过了，等价于成功
            return {"code": 0, "message": "今日已分享过", "duplicate": True}
        return ret

    @staticmethod
    def get_followings(
        session,
        uid: int,
        pn: int = 1,
        ps: int = 50,
        order: str = "desc",
        order_type: str = "attention",
    ) -> dict:
        """
        获取指定用户关注的up主
        uid int 账户uid 默认为本账户 非登录账户只能获取20个*5页
        pn int 页码 默认第一页
        ps int 每页数量 默认50
        order str 排序方式 默认desc
        order_type 排序类型 默认attention
        """
        params = {
            "vmid": uid,
            "pn": pn,
            "ps": ps,
            "order": order,
            "order_type": order_type,
        }
        url = "https://api.bilibili.com/x/relation/followings"
        ret = session.get(url=url, params=params).json()
        return ret

    @staticmethod
    def space_arc_search(
        session,
        uid: int,
        pn: int = 1,
        ps: int = 30,
        tid: int = 0,
        order: str = "pubdate",
        keyword: str = "",
    ) -> dict:
        """
        获取指定up主空间视频投稿信息
        uid int 账户uid 默认为本账户
        pn int 页码 默认第一页
        ps int 每页数量 默认50
        tid int 分区 默认为0(所有分区)
        order str 排序方式 默认pubdate
        keyword str 关键字 默认为空
        """
        params = {
            "mid": uid,
            "pn": pn,
            "Ps": ps,
            "tid": tid,
            "order": order,
            "keyword": keyword,
        }
        url = "https://api.bilibili.com/x/space/arc/search"
        ret = session.get(url=url, params=params).json()
        count = 2
        data_list = [
            {
                "aid": one.get("aid"),
                "cid": 0,
                "title": one.get("title"),
                "owner": one.get("author"),
            }
            for one in (((ret.get("data") or {}).get("list") or {}).get("vlist") or [])[:count]
        ]
        return data_list, count

    @staticmethod
    def elec_pay(session, bili_jct, uid: int, num: int = 50) -> dict:
        """
        用B币给up主充电
        uid int up主uid
        num int 充电电池数量
        """
        url = "https://api.bilibili.com/x/ugcpay/trade/elec/pay/quick"
        post_data = {
            "elec_num": num,
            "up_mid": uid,
            "otype": "up",
            "oid": uid,
            "csrf": bili_jct,
        }
        ret = session.post(url=url, data=post_data).json()
        return ret

    @staticmethod
    def coin_add(session, bili_jct, aid: int, num: int = 1, select_like: int = 1) -> dict:
        """
        给指定 av 号视频投币
        aid int 视频av号
        num int 投币数量
        select_like int 是否点赞
        """
        url = "https://api.bilibili.com/x/web-interface/coin/add"
        post_data = {
            "aid": aid,
            "multiply": num,
            "select_like": select_like,
            "cross_domain": "true",
            "csrf": bili_jct,
        }
        ret = session.post(url=url, data=post_data).json()

        return ret

    @staticmethod
    def live_status(session) -> dict:
        """B站直播获取金银瓜子状态"""
        url = "https://api.live.bilibili.com/pay/v1/Exchange/getStatus"
        try:
            ret = session.get(url=url, timeout=15).json()
        except Exception:
            ret = {}
        if not isinstance(ret, dict):
            ret = {}
        # `data` 为 null 时 `.get("data")` 得到 None，后面的 .get 会炸
        data = ret.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        silver = data.get("silver", 0)
        gold = data.get("gold", 0)
        coin = data.get("coin", 0)
        msg = [
            {"name": "硬币数量", "value": coin},
            {"name": "金瓜子数", "value": gold},
            {"name": "银瓜子数", "value": silver},
        ]
        return msg

    @staticmethod
    def _fetch_region(session, url: str, rid: int, num: int) -> list:
        """请求一个分区接口，把多种返回结构归一成 [aid/cid/title/owner] 列表。

        各接口的字段位置不一致：ranking/v2 在 data.list，popular 在 data.list，
        newlist 在 data.archives。这里都试一遍，取到哪个算哪个。
        """
        try:
            ret = session.get(url=url, timeout=15).json()
        except Exception:
            return []
        data = ret.get("data") or {}
        if not isinstance(data, dict):
            return []
        raw = data.get("list") or data.get("archives") or []
        if not isinstance(raw, list):
            return []
        return [
            {
                "aid": one.get("aid"),
                "cid": one.get("cid"),
                "title": one.get("title"),
                "owner": (one.get("owner") or {}).get("name"),
            }
            for one in raw
            if isinstance(one, dict) and one.get("aid") and one.get("cid")
        ]

    @staticmethod
    def get_region(session, rid=1, num=6) -> list:
        """
        获取 B站分区视频信息

        原实现只打 `/x/web-interface/dynamic/region`，该接口已下线，恒返回
        `{"code": -404, "data": null}`，导致后续的观看/分享/投币全部拿不到视频。
        改成按顺序试多个仍然可用的接口，哪个返回视频就用哪个。

        rid int 分区号
        num int 获取视频数量
        """
        candidates = [
            # ranking/v2：排行榜，字段在 data.list
            f"https://api.bilibili.com/x/web-interface/ranking/v2?rid={rid}&type=all",
            # popular：综合热门，字段在 data.list
            f"https://api.bilibili.com/x/web-interface/popular?ps={num}&pn=1",
            # newlist：最新投稿，字段在 data.archives
            f"https://api.bilibili.com/x/web-interface/newlist?rid={rid}&ps={num}&pn=1",
        ]
        for url in candidates:
            data_list = BiliBili._fetch_region(session, url, rid, num)
            if data_list:
                return data_list[:num]
        return []

    @staticmethod
    def silver2coin(session, bili_jct) -> dict:
        """B站银瓜子换硬币"""
        url = "https://api.live.bilibili.com/xlive/revenue/v1/wallet/silver2coin"
        post_data = {"csrf": bili_jct}
        ret = session.post(url=url, data=post_data).json()
        return ret

    @staticmethod
    def parse_cookie(cookie_text: str) -> dict:
        """把 `k=v; k=v` 解析成 dict。

        原实现是 `{item.split("=")[0]: item.split("=")[1] for item in ...}`，
        遇到空串、`k=`（无值）、或值里带 `=`（base64）都会 IndexError 或截断。
        """
        cookie = {}
        for item in (cookie_text or "").split(";"):
            item = item.strip()
            if not item or "=" not in item:
                continue
            key, _, value = item.partition("=")
            key = key.strip()
            if key:
                cookie[key] = value.strip()
        return cookie

    def main(self):
        bilibili_cookie = self.parse_cookie(self.check_item.get("cookie"))
        if not bilibili_cookie.get("SESSDATA") or not bilibili_cookie.get("bili_jct"):
            return "Cookie 不完整：需要 SESSDATA 和 bili_jct，请重新从浏览器复制"
        bili_jct = bilibili_cookie.get("bili_jct")
        coin_num = self.check_item.get("coin_num", 0)
        coin_type = self.check_item.get("coin_type", 1)
        silver2coin = self.check_item.get("silver2coin", False)
        # 观看视频 300 秒：默认关闭，因为它会往「观看历史」写记录
        watch_video = self.check_item.get("watch_video", False)
        session = requests.session()
        requests.utils.add_dict_to_cookiejar(session.cookies, bilibili_cookie)
        session.headers.update(
            {
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36 Edg/91.0.864.64",
                "Referer": "https://www.bilibili.com/",
                "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
                "Connection": "keep-alive",
            }
        )
        success_count = 0
        uname, uid, is_login, coin, vip_type, current_exp = self.get_nav(session=session)
        if not is_login:
            # 原实现是 `if is_login:` 包住全部逻辑，登录态失效时直接掉出分支、
            # 隐式 return None；而调用方（daily_checkin/main.py:96-97）只要没抛异常
            # 就打 ✅，于是过期 Cookie 也会报「签到成功」。必须抛出才会显示 ❌。
            raise RuntimeError("B站登录态失效（SESSDATA 过期或被风控），请重新获取 Cookie")
        manhua_msg = self.manga_sign(session=session)
        live_msg = self.live_sign(session=session)
        aid_list = self.get_region(session=session)
        vip_privilege_my_ret = self.vip_privilege_my(session=session)
        welfare_list = (vip_privilege_my_ret.get("data") or {}).get("list") or []
        for welfare in welfare_list:
            if welfare.get("state") == 0 and welfare.get("vip_type") == vip_type:
                vip_privilege_receive_ret = self.vip_privilege_receive(
                    session=session,
                    bili_jct=bili_jct,
                    receive_type=welfare.get("type"),
                )
                print(vip_privilege_receive_ret)
        # 用 .get 而不是 x["reason"]：接口字段缺失时不该整条任务崩掉
        coins_av_count = len([one for one in self.get_today_exp(session=session) if one.get("reason") == "视频投币奖励"])
        coin_num = coin_num - coins_av_count
        coin_num = coin_num if coin_num < coin else coin
        if coin_type == 1:
            following_list = self.get_followings(session=session, uid=uid)
            count = 0
            for following in (following_list.get("data") or {}).get("list") or []:
                mid = following.get("mid")
                if mid:
                    tmplist, tmpcount = self.space_arc_search(session=session, uid=mid)
                    aid_list += tmplist
                    count += tmpcount
                    if count > coin_num:
                        print("已获取足够关注用户的视频")
                        break
            else:
                aid_list += self.get_region(session=session)
            for one in aid_list[::-1]:
                print(one)
        if coin_num > 0:
            for aid in aid_list[::-1]:
                if not isinstance(aid, dict) or not aid.get("aid"):
                    continue
                ret = self.coin_add(session=session, aid=aid.get("aid"), bili_jct=bili_jct)
                if not isinstance(ret, dict):
                    ret = {}
                if ret.get("code") == 0:
                    coin_num -= 1
                    print(f"成功给{aid.get('title')}投一个币")
                    success_count += 1
                elif ret.get("code") == 34005:
                    # 34005 投币达到上限；另外 -104 硬币不够了、-111 csrf 失败
                    print(f"投币{aid.get('title')}失败，原因为{ret.get('message')}")
                    continue
                else:
                    print(f"投币{aid.get('title')}失败，原因为{ret.get('message')}，跳过投币")
                    break
                if coin_num <= 0:
                    break
            coin_msg = f"今日成功投币{success_count + coins_av_count}/{self.check_item.get('coin_num', 5)}个"
        else:
            coin_msg = f"今日成功投币{coins_av_count}/{self.check_item.get('coin_num', 5)}个"
        if not aid_list:
            # 所有分区接口都没拿到视频（风控/接口再变），兜底而不是崩
            report_msg = "已跳过（没拿到视频列表）"
            share_msg = "已跳过（没拿到视频列表）"
        else:
            aid = aid_list[0].get("aid")
            cid = aid_list[0].get("cid")
            title = aid_list[0].get("title")
            report_ret = self.report_task(
                session=session, bili_jct=bili_jct, aid=aid, cid=cid, enabled=watch_video
            )
            if report_ret.get("skipped"):
                report_msg = "已关闭（watch_video=false，避免污染观看历史）"
            elif report_ret.get("code") == 0:
                report_msg = f"观看《{title}》300秒"
            else:
                report_msg = f"任务失败({report_ret.get('message')})"
            share_ret = self.share_task(session=session, bili_jct=bili_jct, aid=aid)
            if share_ret.get("code") == 0:
                if share_ret.get("duplicate"):
                    share_msg = f"分享《{title}》（今日已分享过）"
                else:
                    share_msg = f"分享《{title}》成功"
            else:
                share_msg = share_ret.get("message") or "分享失败"
                print(share_msg)
        s2c_msg = "不兑换硬币"
        if silver2coin:
            silver2coin_ret = self.silver2coin(session=session, bili_jct=bili_jct) or {}
            s2c_msg = silver2coin_ret.get("message") or ""
            if silver2coin_ret.get("code") != 0:
                print(s2c_msg)
            else:
                s2c_msg = ""
        live_stats = self.live_status(session=session)
        uname, uid, is_login, new_coin, vip_type, new_current_exp = self.get_nav(session=session)
        today_exp = sum(one.get("delta", 0) for one in self.get_today_exp(session=session))
        # Lv6 封顶：`current_exp` 会超过 28800，此时 `(28800 - exp)` 是负数，
        # 原实现会算出「升级还需: -921天」。封顶就明确说封顶。
        level_cap = 28800
        if new_current_exp is None or new_current_exp >= level_cap:
            update_msg = "已封顶(Lv6)"
        elif today_exp > 0:
            update_msg = f"{(level_cap - new_current_exp) // today_exp}天"
        else:
            update_msg = "今日暂无经验增长"
        msg = [
            {"name": "帐号信息", "value": uname},
            {"name": "漫画签到", "value": manhua_msg},
            {"name": "直播签到", "value": live_msg},
            {"name": "登陆任务", "value": "今日已登陆"},
            {"name": "观看视频", "value": report_msg},
            {"name": "分享任务", "value": share_msg},
            {"name": "瓜子兑换", "value": s2c_msg},
            {"name": "投币任务", "value": coin_msg},
            {"name": "今日经验", "value": today_exp},
            {"name": "当前经验", "value": new_current_exp},
            {"name": "升级还需", "value": update_msg},
            *live_stats,
        ]
        msg = "\n".join([f"{one.get('name')}: {one.get('value')}" for one in msg])
        return msg


if __name__ == "__main__":
    with open(
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json"),
        encoding="utf-8",
    ) as f:
        datas = json.loads(f.read())
    _check_item = datas.get("BILIBILI", [])[0]
    print(BiliBili(check_item=_check_item).main())
