import json
import os

from daily_checkin import CheckIn


def checkin_map():
    result = {}
    for cls in CheckIn.__subclasses__():
        check_name = cls.__name__.upper()
        if check_name:
            result[check_name] = (cls.name, cls)
    return result


checkin_map = checkin_map()

notice_map = {
    "BARK_URL": "",
    "COOLPUSHEMAIL": "",
    "COOLPUSHQQ": "",
    "COOLPUSHSKEY": "",
    "COOLPUSHWX": "",
    "DINGTALK_ACCESS_TOKEN": "",
    "DINGTALK_SECRET": "",
    "FSKEY": "",
    "PUSHPLUS_TOKEN": "",
    "PUSHPLUS_TOPIC": "",
    "QMSG_KEY": "",
    "QMSG_TYPE": "",
    "QYWX_AGENTID": "",
    "QYWX_CORPID": "",
    "QYWX_CORPSECRET": "",
    "QYWX_KEY": "",
    "QYWX_TOUSER": "",
    "QYWX_MEDIA_ID": "",
    "QYWX_ORIGIN": "",
    "SCKEY": "",
    "SENDKEY": "",
    "TG_API_HOST": "",
    "TG_BOT_TOKEN": "",
    "TG_PROXY": "",
    "TG_USER_ID": "",
    "MERGE_PUSH": "",
    "GOTIFY_URL": "",
    "GOTIFY_TOKEN": "",
    "GOTIFY_PRIORITY": "",
    "NTFY_URL": "",
    "NTFY_TOPIC": "",
    "NTFY_PRIORITY": "",
}


# 运行时选项：以前只能靠环境变量，现在优先读 config.json 的这一段。
# 环境变量仍然保留，作为容器/临时覆盖的兜底。
#   log_dir     日志目录，默认 <项目根>/logs
#   db_path     签到记录库，默认 <项目根>/data.db
#   write_db    是否写库，false 可完全关闭
OPTIONS_SECTION = "OPTIONS"

OPTION_DEFAULTS = {
    "log_dir": "",
    "db_path": "",
    "write_db": True,
}


def get_option_info(data):
    """从 config.json 的 OPTIONS 段取运行时选项，缺失项用默认值。

    兼容两种写法：OPTIONS 段（推荐）与顶层散落的同名键。
    """
    result = dict(OPTION_DEFAULTS)
    if isinstance(data, dict):
        options = data.get(OPTIONS_SECTION)
        if isinstance(options, dict):
            for key in OPTION_DEFAULTS:
                if key in options and options[key] not in (None, ""):
                    result[key] = options[key]
        # 顶层也认，方便只想改一项的人少嵌一层
        for key in OPTION_DEFAULTS:
            if key in data and data[key] not in (None, ""):
                result[key] = data[key]
    if not isinstance(result.get("write_db"), bool):
        raw = str(result.get("write_db", "")).strip().lower()
        result["write_db"] = raw not in ("false", "0", "no", "off", "")
    return result


def env2list(key):
    try:
        value = json.loads(os.getenv(key, []).strip()) if os.getenv(key) else []
        if not isinstance(value, list):
            value = []
    except Exception as e:
        print(e)
        value = []
    return value


def env2str(key):
    try:
        value = os.getenv(key, "") if os.getenv(key) else ""
        if isinstance(value, str):
            value = value.strip()
        elif isinstance(value, bool):
            pass
        else:
            value = None
    except Exception as e:
        print(e)
        value = None
    return value


def get_checkin_info(data):
    result = {}
    if isinstance(data, dict):
        for one in checkin_map.keys():
            result[one.lower()] = data.get(one, [])
    else:
        for one in checkin_map.keys():
            result[one.lower()] = env2list(one)
    return result


def get_notice_info(data):
    result = {}
    if isinstance(data, dict):
        for one in notice_map.keys():
            result[one.lower()] = data.get(one, None)
    else:
        for one in notice_map.keys():
            result[one.lower()] = env2str(one)
    return result
