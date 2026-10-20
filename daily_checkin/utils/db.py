"""DailyCheckIn 签到记录库：SQLite 读写工具。

一张表，只记签到情况。别的程序通过调用这个模块或它的 CLI 来读写，不需要懂 daily_checkin。

作为模块使用：
    from daily_checkin.utils import db
    db.init_db()
    db.add_records([{"platform": "V2EX", "platform_name": "V2EX",
                     "status": "ok", "message": "签到成功"}])
    for row in db.query("SELECT * FROM t_checkin_records"):
        print(dict(row))

CLI 用法（`python -m daily_checkin.utils.db ...`）：
    init                                 建表（幂等，可反复执行）
    sql "SELECT * FROM t_checkin_records LIMIT 5"        执行 SQL（默认只读）
    sql --write "DELETE FROM t_checkin_records WHERE id=1"
    echo "SELECT 1" | sql -              从标准输入读 SQL（免引号转义）

    add --platform BILIBILI --platform-name Bilibili --status ok --message "签到成功"
    echo '[{"platform":"V2EX","platform_name":"V2EX","status":"ok"}]' | add --json -

    query --platform BILIBILI --limit 10 便捷查询
    stats                                汇总统计
    paths                                显示库路径

安全约定：
  - `sql` 默认走**只读连接**（file:...?mode=ro），写语句会被拒绝；
    要写必须显式加 --write。别的程序误传一条 DELETE 也不会毁库。
  - 需要拼用户数据时用 `-p k=v` 绑定参数，不要手工拼字符串。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime

# 库文件默认是项目根目录的 data.db；DC_DB 环境变量可覆盖
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DB = os.path.join(_PROJECT_ROOT, "data.db")

# 表结构。改结构只改这里，init 幂等。
TABLE = "t_checkin_records"
LEGACY_TABLE = "checkins"  # 改名前用的表名，init 会自动迁移

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS t_checkin_records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    platform      TEXT    NOT NULL,              -- 配置键名，如 BILIBILI
    platform_name TEXT    NOT NULL,              -- 中文名，如 Bilibili
    account_index INTEGER NOT NULL DEFAULT 0,    -- 同平台多账号的第几个（从 0 起）
    status        TEXT    NOT NULL,              -- ok | fail
    message       TEXT                           -- 模块返回的完整原文
);

CREATE INDEX IF NOT EXISTS idx_t_checkin_records_platform
    ON t_checkin_records(platform, created_at);
CREATE INDEX IF NOT EXISTS idx_t_checkin_records_status
    ON t_checkin_records(status, created_at);
"""

READONLY_PREFIXES = ("select", "with", "explain", "pragma", "table", "values")
WRITE_PREFIXES = (
    "insert",
    "update",
    "delete",
    "create",
    "drop",
    "alter",
    "replace",
    "vacuum",
    "reindex",
    "attach",
    "detach",
    "begin",
    "commit",
)

STATUS_OK = "ok"
STATUS_FAIL = "fail"


# --------------------------------------------------------------------------
# 连接 / 建表
# --------------------------------------------------------------------------


# config.json 里配的库路径。为空表示用 DEFAULT_DB。
# 单独放着而不是直接改 DEFAULT_DB，是为了让 set_default_path(None) 能还原。
_configured_path: str | None = None


def set_default_path(path: str | None) -> None:
    """由 main.py 在读到 config.json 后调用，指定库文件位置。"""
    global _configured_path
    _configured_path = path or None


def db_path(explicit: str | None = None) -> str:
    """库文件路径，优先级从高到低：

    显式参数 > config.json 的 OPTIONS.db_path > 环境变量 DC_DB > data.db

    配置排在环境变量前面，是为了和 main.py 的 LOG_DIR 保持一致 ——
    两处用不同的优先级会让人搞不清到底哪份生效。
    """
    return explicit or _configured_path or os.environ.get("DC_DB") or DEFAULT_DB


def connect(path: str | None = None, readonly: bool = False) -> sqlite3.Connection:
    """打开连接。readonly=True 时走只读 URI，任何写操作都会报错。"""
    path = db_path(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    if readonly:
        conn = sqlite3.connect("file:%s?mode=ro" % path.replace("?", "%3f"), uri=True)
    else:
        conn = sqlite3.connect(path, timeout=15)
        conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _migrate_legacy_table(conn) -> str:
    """把旧表名 checkins 迁到 t_checkin_records。返回一行说明，无需迁移时返回空串。

    在 executescript(SCHEMA) **之前**调用 —— 否则建表语句会先创建新表，
    再改名就冲突了。用 ALTER TABLE RENAME，数据原地保留、原子生效。
    """
    if not _table_exists(conn, LEGACY_TABLE):
        return ""
    if _table_exists(conn, TABLE):
        # 两张表都在（迁移过一半），不擅自动数据，只提示
        return "注意: %s 和 %s 同时存在，未自动处理，请人工确认" % (LEGACY_TABLE, TABLE)
    n = conn.execute("SELECT COUNT(*) FROM %s" % LEGACY_TABLE).fetchone()[0]
    conn.execute("ALTER TABLE %s RENAME TO %s" % (LEGACY_TABLE, TABLE))
    # 改名后索引的名字还是旧的，删掉让 SCHEMA 里的新索引重建
    for (idx,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? "
        "AND name NOT LIKE 'sqlite_%'",
        (TABLE,),
    ).fetchall():
        conn.execute("DROP INDEX IF EXISTS %s" % idx)

    # ALTER TABLE RENAME 只改名字、不会补列。旧表如果缺列（历史版本建的），
    # 后面 CREATE TABLE IF NOT EXISTS 也不会补 —— 表已存在就直接跳过，
    # 结果就是「迁移成功」但一查就报 no such column。这里显式补。
    added = _add_missing_columns(conn)
    conn.commit()
    note = "已迁移旧表 %s → %s（%d 行数据）" % (LEGACY_TABLE, TABLE, n)
    if added:
        note += "，并补齐列 %s" % ", ".join(added)
    return note


# 新表应有的列，迁移时用来比对并补齐。
# 注意 created_at 这里只能用字面量默认值 —— SQLite 的 ALTER TABLE ADD COLUMN
# 不允许非常量默认值（datetime('now') 会报 "Cannot add a column with
# non-constant default"）。补齐后由 add_records 显式写入时间，不依赖默认值。
EXPECTED_COLUMNS = {
    "created_at": "TEXT NOT NULL DEFAULT ''",
    "platform": "TEXT NOT NULL DEFAULT ''",
    "platform_name": "TEXT NOT NULL DEFAULT ''",
    "account_index": "INTEGER NOT NULL DEFAULT 0",
    "status": "TEXT NOT NULL DEFAULT 'ok'",
    "message": "TEXT",
}


def _add_missing_columns(conn) -> list[str]:
    """给已存在的表补齐缺失列，返回补了哪些列。"""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % TABLE)}
    if not existing:
        return []
    added = []
    for name, decl in EXPECTED_COLUMNS.items():
        if name not in existing:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (TABLE, name, decl))
            added.append(name)
    if "created_at" in added:
        # 补出来的列默认是空串，回填成迁移时刻，免得旧行在报告里显示空白
        conn.execute(
            "UPDATE %s SET created_at = datetime('now', 'localtime') WHERE created_at = ''"
            % TABLE
        )
    return added


def init_db(path: str | None = None) -> str:
    """建表（幂等），顺带把旧表名迁过来。返回实际使用的库路径。"""
    path = db_path(path)
    conn = connect(path)
    try:
        note = _migrate_legacy_table(conn)
        if note and not note.startswith("注意"):
            print(note)
        elif note:
            print(note, file=sys.stderr)
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
    return path


def query(sql: str, params=(), path: str | None = None) -> list[sqlite3.Row]:
    """只读查询，返回 Row 列表（可当 dict 用）。"""
    conn = connect(path, readonly=True)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 写入 API
# --------------------------------------------------------------------------


def add_records(rows, path: str | None = None) -> list[int]:
    """批量写入签到记录，rows 是 dict 列表。返回插入的 id 列表。

    每行认这些键：platform, platform_name, account_index, status, message, created_at
    """
    if not rows:
        return []
    conn = connect(path)
    try:
        ids = []
        for row in rows:
            cur = conn.execute(
                "INSERT INTO %s (platform, platform_name, account_index," % TABLE
                + " status, message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(row.get("platform", "")).upper(),
                    row.get("platform_name") or row.get("platform", ""),
                    int(row.get("account_index", 0) or 0),
                    row.get("status", STATUS_OK),
                    row.get("message"),
                    row.get("created_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            ids.append(int(cur.lastrowid))
        conn.commit()
        return ids
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 失败文本识别
#
# main.py 里已经用「抛没抛异常」判定过一轮了，但有些模块把错误当普通文本
# 返回（例如 baiduwp 返回 "签到请求失败: 404 ..." 而不抛异常），那种会被记成 ok。
# 这里补一层基于文本的兜底判定，只对「已判成功」的记录生效。
# --------------------------------------------------------------------------

# 命中即认为该平台本次不成功。保守起见只放明确的失败词。
FAIL_PATTERNS = (
    # 平台返回的业务失败
    "失败",
    "失效",
    "错误",
    "异常",
    "过期",
    "未登录",
    "请先登录",
    "签到请求失败",
    "获取失败",
    "推送失败",
    # 代码层崩溃（模块抛异常时的提示文本）
    "Traceback",
    "Error",
    "Exception",
    "can't encode",
    "can't decode",
    "codec",
    "index out of range",
    "KeyError",
    "TypeError",
    "ValueError",
    "AttributeError",
    "JSONDecodeError",
    "timed out",
    "Max retries",
)

# 上面的关键词会漏掉一类最常见的崩溃：模块把异常吞掉、只把 str(exc) 当结果返回。
# 例如 Bilibili 返回 "'NoneType' object has no attribute 'get'" —— 一个 "Error"
# 字样都没有，于是被误判成 OK。这里补一组 Python 异常原文的正则。
FAIL_REGEXES = (
    r"'?\w+'? object has no attribute",
    r"object is not subscriptable",
    r"object is not iterable",
    r"object is not callable",
    r"unsupported operand type",
    r"takes \d+ positional argument",
    r"missing \d+ required positional argument",
    r"unexpected keyword argument",
    r"referenced before assignment",
    r"is not defined",
    r"not enough values to unpack",
    r"too many values to unpack",
    r"invalid literal for int",
    r"index out of range",
    r"division by zero",
    r"No connection adapters",
    r"ConnectionError",
    r"ReadTimeout",
    r"SSLError",
)
_FAIL_RE = re.compile("|".join(FAIL_REGEXES), re.I)

# 这些词出现时，本行虽然可能带着「失败」字样，但实际含义是「今天已经做过了」，
# 或者「平台已关停该功能」——都不是本次签到失败。
# 例如 Bilibili 的「漫画签到: 签到失败，信息为(不能重复签到~)」，
# 字面有「失败」，真实含义是漫画已经签过了。
BENIGN_PATTERNS = (
    # 已经做过。注意要同时收「已经签到」和「已签到」两种写法：
    # B站漫画返回的是「今日已签到」，只写「已经签到」匹配不到。
    "不能重复签到",
    "请勿重复签到",
    "重复签到",
    "已经签到",
    "已签到",
    "今日已登陆",
    "今日已登录",
    "已领取",
    "重复领取",
    # 平台关停的功能（B站直播签到等），非用户可修复
    "活动已下线",
    "已下线",
    "无法使用",
)

# 即使本行命中了 BENIGN_PATTERNS，只要出现这些「硬失败」特征就仍然算失败。
# 防止「已领取过，但 token 已失效」这类真失败被「已领取」掩盖。
# 注意这里不放 "cookie" 这个裸词：像「已签到，cookie 正常」会被误伤；
# 用 "失效" / "过期" 这类本身就只能表示失败的词。
HARD_FAIL = (
    "Traceback",
    "Error",
    "Exception",
    "can't encode",
    "can't decode",
    "失效",
    "过期",
    "未登录",
    "请先登录",
    "重新登录",
)


def _is_benign_line(line: str) -> bool:
    """这一行是否属于「已做过/功能下线」而应当放过。"""
    if not any(b in line for b in BENIGN_PATTERNS):
        return False
    # 硬失败特征优先于放过
    if any(h in line for h in HARD_FAIL):
        return False
    if _FAIL_RE.search(line):
        return False
    return True


def detect_failure(text: str) -> str:
    """返回命中的失败特征；没命中返回空串。

    按行判断，而不是整段匹配 —— 一个平台返回的是一份多任务清单，
    其中「已经做过」的行不该让整个平台记成失败。

    顺序：先看业务关键词，再看 Python 异常原文的正则。后者能捞住
    「模块把异常吞掉、只把 str(exc) 当结果返回」的情况，
    例如 Bilibili 的 "'NoneType' object has no attribute 'get'"
    —— 一个 'Error' 字样都没有。
    """
    if not text:
        return ""
    for line in str(text).splitlines():
        if not line.strip():
            continue
        if _is_benign_line(line):
            continue
        for pattern in FAIL_PATTERNS:
            if pattern in line:
                return pattern
        match = _FAIL_RE.search(line)
        if match:
            return match.group(0)
    return ""


def find_benign_notes(text: str) -> list[str]:
    """挑出被当成「已做过/已下线」而放过的行，供报告展示，避免静默掩盖。"""
    return [ln.strip() for ln in str(text).splitlines() if ln.strip() and _is_benign_line(ln.strip())]


# --------------------------------------------------------------------------
# 执行 SQL
# --------------------------------------------------------------------------


def leading_keyword(sql: str) -> str:
    """取第一条语句的首个关键词，用于判断读写。"""
    cleaned = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        cleaned.append(stripped)
    text = " ".join(cleaned).strip().lstrip("(").strip()
    return text.split(None, 1)[0].lower() if text else ""


def run_sql(sql: str, path: str | None = None, write: bool = False, params: dict | None = None) -> dict:
    """执行 SQL，返回 {'columns': [...], 'rows': [[...]], 'rowcount': n, 'write': bool}"""
    params = params or {}
    keyword = leading_keyword(sql)
    if not keyword:
        raise ValueError("空 SQL")
    if keyword in WRITE_PREFIXES and not write:
        raise PermissionError("拒绝执行写语句 '%s'：默认只读。要写请显式加 --write。" % keyword)

    conn = connect(path, readonly=not write)
    try:
        cur = conn.cursor()
        try:
            cur.execute(sql, params)
        except sqlite3.ProgrammingError:
            # execute 只能跑一条语句，退回 executescript（多语句场景）
            conn.executescript(sql)
            conn.commit()
            return {"columns": [], "rows": [], "rowcount": -1, "write": True}
        if cur.description:
            columns = [d[0] for d in cur.description]
            rows = [list(r) for r in cur.fetchall()]
            return {"columns": columns, "rows": rows, "rowcount": len(rows), "write": False}
        if write:
            conn.commit()
        return {"columns": [], "rows": [], "rowcount": cur.rowcount, "write": True}
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------


def _cell(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        return "<%d bytes>" % len(value)
    return str(value)


def print_table(columns: list[str], rows: list[list]) -> None:
    """对齐打印，类似 sqlite3 CLI 的 table 模式。"""
    if not columns:
        return
    widths = [len(c) for c in columns]
    body = [[_cell(v) for v in row] for row in rows]
    for row in body:
        for i, v in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(str(v).replace("\n", "\\n")))
    line = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    print(line)
    print("| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(columns)) + " |")
    print(line)
    for row in body:
        cells = [v.replace("\n", "\\n").ljust(widths[i]) for i, v in enumerate(row)]
        print("| " + " | ".join(cells) + " |")
    print(line)
    print("(%d 行)" % len(body))


def emit(result: dict, as_json: bool) -> None:
    if as_json:
        # 别的程序最省事的形状：对象数组
        if result["columns"]:
            payload = [dict(zip(result["columns"], row)) for row in result["rows"]]
        else:
            payload = {"rowcount": result["rowcount"], "write": result["write"]}
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=_cell))
        return
    if result["columns"]:
        print_table(result["columns"], result["rows"])
    else:
        print("影响 %s 行" % result["rowcount"])


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def read_sql_arg(sql_arg: str | None) -> str:
    if sql_arg and sql_arg != "-":
        return sql_arg
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit('没有 SQL。用法：db sql "SELECT ..."  或  echo SQL | db sql -')


def parse_params(pairs) -> dict:
    result = {}
    for item in pairs or []:
        if "=" not in item:
            raise SystemExit("-p 需要 k=v 格式，收到: %s" % item)
        key, value = item.split("=", 1)
        result[key.lstrip(":")] = value
    return result


def load_json_arg(value: str):
    """--json 既接受字面 JSON，也接受「-」从 stdin 读，也接受文件路径。"""
    if value in ("-", ""):
        return json.loads(sys.stdin.read() or "null")
    if os.path.exists(value):
        with open(value, encoding="utf-8") as fh:
            return json.load(fh)
    return json.loads(value)


def cmd_init(args) -> int:
    path = init_db(args.db)
    print("库已就绪: %s" % path)
    for row in query(
        "SELECT name, type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name",
        path=path,
    ):
        print("  %-7s %s" % (row["type"], row["name"]))
    return 0


def cmd_paths(args) -> int:
    path = db_path(args.db)
    print("库路径      : %s" % path)
    print("存在        : %s" % os.path.exists(path))
    print("环境变量    : DC_DB=%s" % (os.environ.get("DC_DB") or "(未设置)"))
    print("配置来源    : %s" % (_configured_path or "(未配置，用默认值)"))
    print("项目根      : %s" % _PROJECT_ROOT)
    return 0


def cmd_sql(args) -> int:
    sql = read_sql_arg(args.sql)
    try:
        result = run_sql(sql, path=args.db, write=args.write, params=parse_params(args.param))
    except PermissionError as exc:
        print("错误: %s" % exc, file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print("SQL 错误: %s" % exc, file=sys.stderr)
        return 3
    emit(result, args.json)
    return 0


def cmd_add(args) -> int:
    init_db(args.db)
    if args.json:
        payload = load_json_arg(args.json)
        rows = payload if isinstance(payload, list) else [payload]
    else:
        if not args.platform:
            raise SystemExit("需要 --platform（或用 --json）")
        if args.status not in (STATUS_OK, STATUS_FAIL):
            raise SystemExit("--status 只能是 ok 或 fail")
        rows = [
            {
                "platform": args.platform,
                "platform_name": args.platform_name or args.platform,
                "account_index": args.account_index,
                "status": args.status,
                "message": args.message,
            }
        ]
    ids = add_records(rows, path=args.db)
    print(json.dumps({"ids": ids}, ensure_ascii=False) if args.json_out else "已写入 id=%s" % ids)
    return 0


def cmd_query(args) -> int:
    where, params = [], []
    if args.platform:
        where.append("platform = ?")
        params.append(args.platform.upper())
    if args.status:
        where.append("status = ?")
        params.append(args.status)
    if args.since:
        where.append("created_at >= ?")
        params.append(args.since)
    sql = "SELECT * FROM %s" % TABLE
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(args.limit)
    try:
        rows = query(sql, tuple(params), path=args.db)
    except sqlite3.Error as exc:
        print("SQL 错误: %s" % exc, file=sys.stderr)
        return 3
    data = [dict(r) for r in rows]
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        columns = (
            list(data[0].keys())
            if data
            else ["platform", "platform_name", "account_index", "status", "message"]
        )
        print_table(columns, [[r.get(c) for c in columns] for r in data])
    return 0


def cmd_stats(args) -> int:
    init_db(args.db)
    print("== 按平台汇总 ==")
    rows = query(
        (
            "SELECT platform, platform_name, COUNT(*) AS total,"
            " SUM(status='ok') AS ok_count, SUM(status='fail') AS fail_count,"
            " MAX(created_at) AS last_at"
            " FROM %s GROUP BY platform, platform_name"
            " ORDER BY fail_count DESC, total DESC"
        )
        % TABLE,
        path=args.db,
    )
    cols = ("platform", "platform_name", "total", "ok_count", "fail_count", "last_at")
    print_table(list(cols), [[r[c] for c in cols] for r in rows])
    print()
    print("== 最近 10 条失败 ==")
    bad = query(
        (
            "SELECT created_at, platform_name, substr(message, 1, 60) AS message"
            " FROM %s WHERE status='fail' ORDER BY id DESC LIMIT 10"
        )
        % TABLE,
        path=args.db,
    )
    cols = ("created_at", "platform_name", "message")
    print_table(list(cols), [[r[c] for c in cols] for r in bad])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="daily_checkin.utils.db",
        description="DailyCheckIn 签到记录库（SQLite）读写工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--db", help="库文件路径（默认 data.db，可用环境变量 DC_DB 覆盖）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="建表（幂等）").set_defaults(func=cmd_init)
    sub.add_parser("paths", help="显示库路径").set_defaults(func=cmd_paths)

    p = sub.add_parser("sql", help="执行 SQL（默认只读）")
    p.add_argument("sql", nargs="?", help="SQL 语句；用 - 或省略则从 stdin 读")
    p.add_argument("--write", action="store_true", help="允许写语句（INSERT/UPDATE/DELETE/DDL）")
    p.add_argument("-p", "--param", action="append", help="绑定参数 k=v，可重复；SQL 里写 :k")
    p.add_argument("--json", action="store_true", help="以 JSON 输出（便于别的程序解析）")
    p.set_defaults(func=cmd_sql)

    p = sub.add_parser("add", help="写入签到记录（支持 --json 批量）")
    p.add_argument("--platform", help="配置键名，如 BILIBILI")
    p.add_argument("--platform-name", help="中文名，如 Bilibili")
    p.add_argument("--account-index", type=int, default=0)
    p.add_argument("--status", default=STATUS_OK, help="ok | fail")
    p.add_argument("--message")
    p.add_argument("--json", help="JSON 对象/数组；用 - 从 stdin 读；也接受文件路径")
    p.add_argument("--json-out", action="store_true", help="以 JSON 输出结果")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("query", help="便捷查询")
    p.add_argument("--platform")
    p.add_argument("--status", choices=[STATUS_OK, STATUS_FAIL])
    p.add_argument("--since", help="created_at ≥ 该值，如 2026-10-19")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_query)

    sub.add_parser("stats", help="汇总统计").set_defaults(func=cmd_stats)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
