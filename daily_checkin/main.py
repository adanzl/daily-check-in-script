import argparse
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from daily_checkin.__version__ import __version__
from daily_checkin.configs import (
    OPTION_DEFAULTS,
    checkin_map,
    get_checkin_info,
    get_notice_info,
    get_option_info,
)
from daily_checkin.utils import db
from daily_checkin.utils.message import push_message

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_LOG_DIR = os.path.join(_PROJECT_ROOT, "logs")

# 运行时选项，config.json 的 OPTIONS 段会在 check_config() 里填充到这里。
# 读配置之前用的就是默认值，所以模块级也要有一份。
OPTIONS = dict(OPTION_DEFAULTS)

# 日志目录：config.json 的 OPTIONS.log_dir > DC_LOG_DIR > <项目根>/logs。
# 保留成模块级变量是因为 _write_logs() 直接用，测试也靠它改目录。
LOG_DIR = os.environ.get("DC_LOG_DIR") or _DEFAULT_LOG_DIR


# check_config() 读到的 config.json 内容，供 _resolve_options() 反复计算
_CONFIG_DATA = {}

# 命令行覆盖项。优先级最高，但写在这里而不是直接改 OPTIONS，
# 因为 check_config() 之后会重新解析一遍配置，直接改会被冲掉。
_CLI_OVERRIDES = {"log_dir": None, "db_path": None, "write_db": None}


def _resolve_options() -> None:
    """按优先级算出最终选项：命令行 > config.json > 环境变量 > 默认值。

    统一在这里算，避免 LOG_DIR 和 db_path 各用一套优先级 ——
    不然会出现「日志写到 A、库写到 B」，很难排查。
    """
    global LOG_DIR
    OPTIONS.clear()
    OPTIONS.update(get_option_info(_CONFIG_DATA))
    for key, value in _CLI_OVERRIDES.items():
        if value is not None:
            OPTIONS[key] = value
    LOG_DIR = OPTIONS.get("log_dir") or os.environ.get("DC_LOG_DIR") or _DEFAULT_LOG_DIR
    # 库路径为空时要让 db 走自己的默认值，不能传空串
    db.set_default_path(OPTIONS.get("db_path") or None)


def _apply_options(data) -> None:
    """config.json 读完后调用，记住配置内容并重算选项。"""
    global _CONFIG_DATA
    _CONFIG_DATA = data if isinstance(data, dict) else {}
    _resolve_options()


def _apply_cli_options(args) -> None:
    """把命令行参数并进覆盖表，然后重算选项。"""
    _CLI_OVERRIDES["log_dir"] = getattr(args, "log_dir", None) or None
    _CLI_OVERRIDES["db_path"] = getattr(args, "db_path", None) or None
    # --no-db 只置 False，不写 True：没传时要保持 None 表示「不干预」
    _CLI_OVERRIDES["write_db"] = False if getattr(args, "no_db", False) else None
    _resolve_options()

# Cookie / token 这类长串不该落进日志文件。命中即打码，只留头尾便于排查。
_SECRET_KEYS = (
    "cookie",
    "sessdata",
    "bili_jct",
    "bduss",
    "refresh_token",
    "access_token",
    "sckey",
    "sendkey",
    "tg_bot_token",
    "qywx_corpsecret",
    "fskey",
    "bark_url",
)
_SECRET_RE = re.compile(
    r"(?i)\b(" + "|".join(re.escape(k) for k in _SECRET_KEYS) + r")\b\s*[=:]\s*([^\s;,]{8,})"
)


def mask_secrets(text: str) -> str:
    """把日志里的 Cookie / token 打码，保留头尾各 4 位。"""
    if not text:
        return text

    def _repl(match):
        value = match.group(2)
        if len(value) <= 8:
            return match.group(0)
        return f"{match.group(1)}={value[:4]}***{value[-4:]}"

    return _SECRET_RE.sub(_repl, text)


class _Tee:
    """把 stdout 同时写到原终端和一个缓冲区，用于落盘。"""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for stream in self.streams:
            stream.write(data)
        return len(data)

    def flush(self):
        for stream in self.streams:
            stream.flush()

    def isatty(self):
        return False


def _write_logs(started_at: str, elapsed: int, raw: str, results: list, error: str = "") -> str:
    """写两份日志，返回原始日志路径。

    raw-YYYYMMDD.log  —— 完整 stdout，按次追加，带分隔头
    check_in.log      —— 每次运行一行摘要（成功/失败数 + 各平台状态）
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    raw_path = os.path.join(LOG_DIR, "raw-%s.log" % time.strftime("%Y%m%d"))
    with open(raw_path, "a", encoding="utf-8") as fh:
        fh.write("\n\n########## %s ##########\n" % started_at)
        fh.write(mask_secrets(raw))

    ok = [one for one in results if one["ok"]]
    bad = [one for one in results if not one["ok"]]
    summary_path = os.path.join(LOG_DIR, "check_in.log")
    line = "%s | 耗时 %ss | 成功 %d | 失败 %d | %s\n" % (
        started_at,
        elapsed,
        len(ok),
        len(bad),
        " ".join("%s:%s" % (one["name"], "ok" if one["ok"] else "fail") for one in results)
        or "无任务（未找到 config.json、格式错误，或任务全部被 --exclude/占位符跳过）",
    )
    with open(summary_path, "a", encoding="utf-8") as fh:
        fh.write(line)
        for one in bad:
            first_line = str(one["message"]).strip().splitlines()[0] if str(one["message"]).strip() else ""
            fh.write("    [FAIL] %s#%d %s\n" % (one["name"], one["index"] + 1, mask_secrets(first_line)[:200]))
        if error:
            fh.write("    [ERROR] %s\n" % mask_secrets(error).splitlines()[0][:200])
    return raw_path


def _write_db(started_at: str, results: list, error: str = "") -> tuple[int, str]:
    """把本次结果写进 SQLite，返回 (写入条数, 错误信息)。

    写库失败不应影响签到本身 —— 所有异常都收敛成一条提示。
    config.json 的 OPTIONS.write_db=false 可关闭；DC_NO_DB=1 仍然有效。
    """
    if not OPTIONS.get("write_db", True):
        return 0, ""
    if os.environ.get("DC_NO_DB") == "1":
        return 0, ""
    if not results:
        return 0, ""
    try:
        db.init_db()
    except Exception as exc:  # noqa: BLE001 - 建表失败也要能继续
        return 0, "建表失败: %s: %s" % (type(exc).__name__, exc)

    # 同一平台多账号按出现顺序编号
    seen = {}
    rows = []
    for one in results:
        name = one["name"]
        index = seen.get(name, 0)
        seen[name] = index + 1
        message = str(one.get("message") or "")
        status = db.STATUS_OK if one["ok"] else db.STATUS_FAIL
        # main.py 只按「抛没抛异常」判定，有些模块把错误当普通文本返回
        # （例如 baiduwp 的 "签到请求失败: 404"），这里用文本兜底再判一次。
        if status == db.STATUS_OK:
            hit = db.detect_failure(message)
            if hit:
                status = db.STATUS_FAIL
                message = "%s\n（按失败文本判定: %s）" % (message, hit)
        rows.append(
            {
                "platform": one.get("platform") or name,
                "platform_name": name,
                "account_index": index,
                "status": status,
                "message": message,
                "created_at": started_at,
            }
        )
    try:
        ids = db.add_records(rows)
    except Exception as exc:  # noqa: BLE001 - 写库失败不影响签到
        return 0, "写入失败: %s: %s" % (type(exc).__name__, exc)
    if error:
        return len(ids), "本次运行有整体异常: %s" % error
    return len(ids), ""


# 查库用的旗标。给出任意一个就只查库、不跑签到。
_DB_ACTIONS = {
    "db_init": "init",
    "db_paths": "paths",
    "db_stats": "stats",
    "db_query": "query",
    "db_sql": "sql",
}


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(
        epilog=(
            "查库示例：\n"
            "  dailycheckin --db-stats\n"
            "  dailycheckin --db-query --db-platform BILIBILI --db-limit 10\n"
            "  dailycheckin --db-query --db-status fail\n"
            "  dailycheckin --db-sql \"SELECT * FROM t_checkin_records LIMIT 5\"\n"
            "  dailycheckin --db-init\n"
            "  等价的独立命令：python -m daily_checkin.utils.db <init|paths|stats|query|sql|add>"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--include", nargs="+", help="任务执行包含的任务列表")
    parser.add_argument("--exclude", nargs="+", help="任务执行排除的任务列表")
    # 以下三项优先级高于 config.json 的 OPTIONS，也高于环境变量
    parser.add_argument("--db-path", metavar="PATH", help="签到记录库路径（覆盖 OPTIONS.db_path / DC_DB）")
    parser.add_argument("--no-db", action="store_true", help="本次不写签到记录库（同 OPTIONS.write_db=false）")
    parser.add_argument("--log-dir", metavar="DIR", help="运行日志目录（覆盖 OPTIONS.log_dir / DC_LOG_DIR）")

    # 查库：给出任意一个 --db-* 就只读库、不跑签到
    g = parser.add_argument_group("查库（给了任意一项就只查库，不跑签到）")
    g.add_argument("--db-init", action="store_true", help="建表（幂等）")
    g.add_argument("--db-paths", action="store_true", help="显示库位置")
    g.add_argument("--db-stats", action="store_true", help="各平台成功/失败汇总")
    g.add_argument("--db-query", action="store_true", help="查询签到记录，配合下面几个筛选条件")
    g.add_argument("--db-sql", metavar="SQL", help="执行 SQL；默认只读，要写加 --db-write")
    g.add_argument("--db-write", action="store_true", help="配合 --db-sql 允许写语句")
    g.add_argument("--db-platform", help="筛选平台，如 BILIBILI")
    g.add_argument("--db-status", choices=["ok", "fail"], help="筛选状态")
    g.add_argument("--db-since", help="created_at ≥ 该值，如 2026-10-19")
    g.add_argument("--db-limit", type=int, default=20, help="查询条数上限（默认 20）")
    g.add_argument("--db-json", action="store_true", help="以 JSON 输出（便于别的程序解析）")
    return parser.parse_args(argv)


def _db_action(args):
    """返回本次要执行的查库动作；没给任何 --db-* 时返回 None。"""
    hits = [name for flag, name in _DB_ACTIONS.items() if getattr(args, flag, None)]
    if len(hits) > 1:
        raise SystemExit("参数冲突：%s 只能选一个" % " / ".join(hits))
    return hits[0] if hits else None


def _run_db_command(args, action: str) -> int:
    """把命令行参数转成 db.py 里 cmd_* 认的 Namespace，然后调用。"""
    ns = argparse.Namespace(
        db=args.db_path,
        sql=getattr(args, "db_sql", None),
        write=getattr(args, "db_write", False),
        param=[],
        json=getattr(args, "db_json", False),
        platform=getattr(args, "db_platform", None),
        status=getattr(args, "db_status", None),
        since=getattr(args, "db_since", None),
        limit=getattr(args, "db_limit", 20),
    )
    func = {
        "init": db.cmd_init,
        "paths": db.cmd_paths,
        "stats": db.cmd_stats,
        "query": db.cmd_query,
        "sql": db.cmd_sql,
    }[action]
    return func(ns)


def check_config(task_list):
    config_path = None
    config_path_list = []
    for one_path in [
        "/ql/scripts/config.json",
        "config.json",
        "../config.json",
        "./config/config.json",
        "../config/config.json",
        "/config.json",
    ]:
        _config_path = os.path.join(os.getcwd(), one_path)
        if os.path.exists(_config_path):
            config_path = os.path.normpath(_config_path)
            break
        config_path_list.append(os.path.normpath(os.path.dirname(_config_path)))
    if config_path:
        print("使用配置文件路径:", config_path)
        with open(config_path, encoding="utf-8") as f:
            try:
                data = json.load(f)
            except Exception:
                print("Json 格式错误，请检查 config.json 文件格式是否正确！")
                return False, False
        try:
            _apply_options(data)
            notice_info = get_notice_info(data=data)
            _check_info = get_checkin_info(data=data)
            check_info = {}
            for one_check in checkin_map.keys():
                if one_check in task_list:
                    if _check_info.get(one_check.lower()):
                        for _, check_item in enumerate(_check_info.get(one_check.lower(), [])):
                            if "xxxxxx" not in str(check_item) and "多账号" not in str(check_item):
                                if one_check.lower() not in check_info.keys():
                                    check_info[one_check.lower()] = []
                                check_info[one_check.lower()].append(check_item)
            return notice_info, check_info
        except Exception as e:
            print(e)
            return False, False
    else:
        print("未找到 config.json 配置文件\n请在下方任意目录中添加「config.json」文件:\n" + "\n".join(config_path_list))
        return False, False


def _checkin_impl(argv=None):
    """真正的签到流程，返回每个账号的结果列表。日志由 checkin() 负责。"""
    start_time = time.time()
    # Python 3.12 起 datetime.utcnow() 已弃用；用带时区的写法等价替换（固定 UTC+8）
    utc_time = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    print(f"当前时间: {utc_time}\n当前版本: {__version__}")
    args = parse_arguments(argv)
    # 先按命令行算一遍 —— 配置文件还没读到，但临时目录/--no-db 要立刻生效
    _apply_cli_options(args)
    include = args.include
    exclude = args.exclude
    if not include:
        include = list(checkin_map.keys())
    else:
        include = [one for one in include if one in checkin_map.keys()]
    if not exclude:
        exclude = []
    else:
        exclude = [one for one in exclude if one in checkin_map.keys()]
    task_list = list(set(include) - set(exclude))
    notice_info, check_info = check_config(task_list)
    results = []
    if check_info:
        task_name_str = "\n".join(
            [f"「{checkin_map.get(one.upper())[0]}」账号数 : {len(value)}" for one, value in check_info.items()]
        )
        print(f"\n---------- 本次执行签到任务如下 ----------\n\n{task_name_str}\n\n")
        content_list = []
        for one_check, check_list in check_info.items():
            check_name, check_func = checkin_map.get(one_check.upper())
            print(f"----------开始执行「{check_name}」签到----------")
            for index, check_item in enumerate(check_list):
                try:
                    msg = check_func(check_item).main()
                    if msg is None or not str(msg).strip():
                        # main() 什么都不返回 = 没有可上报的签到结果。
                        # 原实现只要没抛异常就打 ✅，于是「Cookie 过期」「一个任务都没跑」
                        # 也会显示成功。这里改成显式失败并说明原因。
                        raise RuntimeError("签到未返回任何结果（可能是 Cookie 失效或任务全部跳过）")
                    content_list.append(f"「{check_name}」\n{msg}")
                    results.append(
                        {
                            "name": check_name,
                            "platform": one_check.upper(),
                            "index": index,
                            "ok": True,
                            "message": msg,
                        }
                    )
                    print(f"第 {index + 1} 个账号: ✅✅✅✅✅")
                except Exception as e:
                    content_list.append(f"「{check_name}」\n{e}")
                    results.append(
                        {
                            "name": check_name,
                            "platform": one_check.upper(),
                            "index": index,
                            "ok": False,
                            "message": str(e),
                        }
                    )
                    print(f"第 {index + 1} 个账号: ❌❌❌❌❌\n{e}")
        print("\n\n")
        try:
            url = "https://pypi.org/pypi/dailycheckin/json"
            latest_version = requests.get(url=url, timeout=30).json()["info"]["version"]
        except Exception:
            print("获取最新版本失败")
            latest_version = "0.0.0"
        content_list.append(
            f"开始时间: {utc_time}\n"
            f"任务用时: {int(time.time() - start_time)} 秒\n"
            f"当前版本: {__version__}\n"
            f"最新版本: {latest_version}\n"
            f"项目地址: https://github.com/Sitoi/dailycheckin"
        )
        push_message(content_list=content_list, notice_info=notice_info)
        return results
    return results


def checkin(argv=None):
    """带日志的签到入口 —— console_scripts 和服务器包装器都调这个。

    为什么不把日志放在 __main__：`setup.py` 的 entry_points 指向
    `daily_checkin.main:checkin`，服务器上的 `run.py` 也是 `dc_main.checkin()`。
    两条真实路径都不经过 __main__，日志只挂在 __main__ 会一次都写不出来。
    """
    # 给了任意 --db-* 就只查库，不写日志也不跑签到。
    args = parse_arguments(argv)
    action = _db_action(args)
    if action:
        return _run_db_command(args, action)

    started_at = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    t0 = time.time()
    buffer = io.StringIO()
    original_stdout = sys.stdout
    error = ""
    try:
        sys.stdout = _Tee(original_stdout, buffer)
        results = _checkin_impl(argv) or []
    except Exception as exc:  # noqa: BLE001 - 顶层兜底，日志要留下来
        import traceback

        error = "%s: %s" % (type(exc).__name__, exc)
        buffer.write("\n" + traceback.format_exc())
    finally:
        sys.stdout = original_stdout

    elapsed = int(time.time() - t0)
    raw = buffer.getvalue()
    try:
        raw_path = _write_logs(started_at, elapsed, raw, results, error)
        print(f"运行日志已写入: {raw_path}")
    except Exception as exc:  # noqa: BLE001 - 日志失败不应影响签到结果
        print(f"写日志失败: {type(exc).__name__}: {exc}")

    try:
        written, db_note = _write_db(started_at, results, error)
        if written:
            print(f"已写入数据库 {written} 条记录")
        elif db_note:
            print(f"写库未完成: {db_note}")
    except Exception as exc:  # noqa: BLE001 - 写库失败不应影响签到结果
        print(f"写库异常: {type(exc).__name__}: {exc}")
    if error:
        print(f"程序异常: {error}")
        return 1
    return 0


def run(argv=None):
    """兼容旧入口：checkin() 已经含日志，这里只负责把返回值/异常转成退出码。

    注意 `return checkin()` 不能写成光调 `checkin()`：checkin() 查到库操作失败
    会返回 2（比如只读模式下拒绝 DELETE），吞掉返回值就等于永远成功。
    """
    try:
        return checkin(argv)
    except Exception as exc:  # noqa: BLE001 - 交给上层日志/调度器判断
        print(f"程序异常: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(run())
