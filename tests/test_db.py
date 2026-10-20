"""SQLite 签到记录库的测试。

重点覆盖：
1. 建表幂等，旧表名 checkins 能自动迁移；
2. add_records / query / stats 的基本读写；
3. sql 默认只读，写语句必须显式 --write；
4. detect_failure 能把「模块把错误当文本返回」这种情况认出来；
5. main.py 的 _write_db 会把文本失败改判成 fail。
"""

import io
import os
import sys
import tempfile
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from daily_checkin.utils import db


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tmp_db():
    return os.path.join(tempfile.mkdtemp(prefix="dc-db-"), "t.db")


# ------------------------------------------------------------------ 建表 / 迁移
def test_init_is_idempotent():
    path = _tmp_db()
    db.init_db(path)
    db.init_db(path)  # 第二次不应报错
    rows = db.query("SELECT name FROM sqlite_master WHERE type='table'", path=path)
    assert db.TABLE in {r["name"] for r in rows}


def test_legacy_table_migrated():
    path = _tmp_db()
    conn = db.connect(path)
    conn.execute("CREATE TABLE checkins (id INTEGER PRIMARY KEY, platform TEXT, message TEXT)")
    conn.execute("INSERT INTO checkins (platform, message) VALUES ('OLD', '旧数据')")
    conn.commit()
    conn.close()

    db.init_db(path)
    rows = db.query("SELECT * FROM %s" % db.TABLE, path=path)
    assert len(rows) == 1
    assert rows[0]["message"] == "旧数据"
    left = db.query("SELECT name FROM sqlite_master WHERE type='table' AND name='checkins'", path=path)
    assert not left, "旧表应已被改名"


# ------------------------------------------------------------------ 读写
def test_add_and_query():
    path = _tmp_db()
    db.init_db(path)
    ids = db.add_records(
        [
            {"platform": "bilibili", "platform_name": "Bilibili", "status": "ok", "message": "成功"},
            {"platform": "V2EX", "platform_name": "V2EX", "status": "fail", "message": "失败"},
        ],
        path=path,
    )
    assert len(ids) == 2
    rows = db.query("SELECT * FROM %s ORDER BY id" % db.TABLE, path=path)
    assert rows[0]["platform"] == "BILIBILI", "platform 应被转成大写"
    assert rows[1]["status"] == "fail"


def test_add_empty_returns_empty():
    assert db.add_records([], path=_tmp_db()) == []


def test_account_index_default_and_explicit():
    path = _tmp_db()
    db.init_db(path)
    db.add_records([{"platform": "A", "platform_name": "A"}], path=path)
    db.add_records([{"platform": "A", "platform_name": "A", "account_index": 3}], path=path)
    rows = db.query("SELECT account_index FROM %s ORDER BY id" % db.TABLE, path=path)
    assert [r["account_index"] for r in rows] == [0, 3]


# ------------------------------------------------------------------ SQL 保护
def test_sql_readonly_by_default():
    path = _tmp_db()
    db.init_db(path)
    try:
        db.run_sql("DELETE FROM t_checkin_records", path=path)
    except PermissionError:
        return
    raise AssertionError("写语句在默认只读模式下应被拒绝")


def test_sql_write_with_flag():
    path = _tmp_db()
    db.init_db(path)
    db.add_records([{"platform": "A", "platform_name": "A"}], path=path)
    result = db.run_sql("DELETE FROM t_checkin_records", path=path, write=True)
    assert result["write"] is True
    assert db.query("SELECT * FROM %s" % db.TABLE, path=path) == []


def test_leading_keyword_skips_comments():
    assert db.leading_keyword("-- 注释\n SELECT 1") == "select"
    assert db.leading_keyword("(select 1)") == "select"
    assert db.leading_keyword("") == ""


def test_empty_sql_rejected():
    try:
        db.run_sql("   ", path=_tmp_db())
    except ValueError:
        return
    raise AssertionError("空 SQL 应抛 ValueError")


# ------------------------------------------------------------------ 失败识别
def test_detect_failure_business_words():
    assert db.detect_failure("签到请求失败: 404") == "失败"
    assert db.detect_failure("Cookie 已失效") == "失效"


def test_detect_failure_python_exception_text():
    """模块吞掉异常、只把 str(exc) 返回时也要认出来。"""
    hit = db.detect_failure("'NoneType' object has no attribute 'get'")
    assert "object has no attribute" in hit


def test_detect_failure_benign_skipped():
    """「已签到」「活动已下线」不算失败，否则每天都误报。"""
    assert db.detect_failure("今日已签到") == ""
    assert db.detect_failure("漫画签到: 签到失败，信息为(不能重复签到~)") == ""
    assert db.detect_failure("活动已下线，无需签到") == ""


def test_detect_failure_hard_fail_wins_over_benign():
    """「已领取过，但 token 已失效」是真失败，不能被「已领取」掩盖。"""
    assert db.detect_failure("已领取过，但 token 已失效") != ""


def test_detect_failure_none():
    assert db.detect_failure("") == ""
    assert db.detect_failure(None) == ""


def test_find_benign_notes():
    notes = db.find_benign_notes("今日已签到\n真的失败了")
    assert len(notes) == 1
    assert "今日已签到" in notes[0]


# ------------------------------------------------------------------ main.py 接入
def test_write_db_marks_text_failure_as_fail():
    """main() 没抛异常但返回「失败」文本时，库里要记成 fail。"""
    import daily_checkin.main as m

    path = _tmp_db()
    old = db.db_path
    db.db_path = lambda explicit=None: path
    try:
        written, note = m._write_db(
            "2026-10-20 10:00:00",
            [
                {"name": "Bilibili", "platform": "BILIBILI", "index": 0, "ok": True, "message": "签到成功"},
                {"name": "百度网盘", "platform": "BAIDUWP", "index": 0, "ok": True, "message": "签到请求失败: 404"},
            ],
        )
    finally:
        db.db_path = old
    assert written == 2
    rows = db.query("SELECT platform, status FROM %s ORDER BY id" % db.TABLE, path=path)
    assert rows[0]["status"] == "ok"
    assert rows[1]["status"] == "fail", "文本失败应被改判成 fail"


def test_write_db_numbers_multi_account():
    import daily_checkin.main as m

    path = _tmp_db()
    old = db.db_path
    db.db_path = lambda explicit=None: path
    try:
        m._write_db(
            "2026-10-20 10:00:00",
            [
                {"name": "Bilibili", "platform": "BILIBILI", "index": 0, "ok": True, "message": "一号"},
                {"name": "Bilibili", "platform": "BILIBILI", "index": 1, "ok": True, "message": "二号"},
            ],
        )
    finally:
        db.db_path = old
    rows = db.query("SELECT account_index FROM %s ORDER BY id" % db.TABLE, path=path)
    assert [r["account_index"] for r in rows] == [0, 1]


def test_write_db_disabled_by_env():
    import daily_checkin.main as m

    os.environ["DC_NO_DB"] = "1"
    try:
        written, note = m._write_db("2026-10-20 10:00:00", [{"name": "A", "ok": True, "message": "x"}])
    finally:
        os.environ.pop("DC_NO_DB", None)
    assert written == 0


def test_write_db_empty_results():
    import daily_checkin.main as m

    assert m._write_db("2026-10-20 10:00:00", []) == (0, "")


# ------------------------------------------------------------------ config.json 选项
def test_options_from_config_section():
    """OPTIONS 段能覆盖 log_dir / db_path / write_db。"""
    from daily_checkin.configs import get_option_info

    info = get_option_info(
        {
            "OPTIONS": {
                "log_dir": "/tmp/lg",
                "db_path": "/tmp/x.db",
                "write_db": False,
            }
        }
    )
    assert info["log_dir"] == "/tmp/lg"
    assert info["db_path"] == "/tmp/x.db"
    assert info["write_db"] is False


def test_options_defaults_when_missing():
    from daily_checkin.configs import get_option_info

    info = get_option_info({})
    assert info["log_dir"] == ""
    assert info["db_path"] == ""
    assert info["write_db"] is True


def test_options_accept_top_level_keys():
    """不想嵌一层的人可以直接写顶层。"""
    from daily_checkin.configs import get_option_info

    info = get_option_info({"db_path": "/tmp/top.db"})
    assert info["db_path"] == "/tmp/top.db"


def test_options_write_db_string_false():
    """JSON 里写成字符串 "false" 也要认。"""
    from daily_checkin.configs import get_option_info

    assert get_option_info({"OPTIONS": {"write_db": "false"}})["write_db"] is False
    assert get_option_info({"OPTIONS": {"write_db": "no"}})["write_db"] is False
    assert get_option_info({"OPTIONS": {"write_db": "true"}})["write_db"] is True


def test_db_set_default_path():
    """main.py 读到 config 后调用，db_path() 要跟着变。"""
    old = db.db_path()
    try:
        db.set_default_path("/tmp/from-config.db")
        assert db.db_path() == "/tmp/from-config.db"
        # 显式参数优先级仍然最高
        assert db.db_path("/tmp/explicit.db") == "/tmp/explicit.db"
        # 传 None 还原成默认值
        db.set_default_path(None)
        assert db.db_path() != "/tmp/from-config.db"
    finally:
        db.set_default_path(None)


def test_apply_options_sets_log_dir():
    import daily_checkin.main as m

    old = m.LOG_DIR
    try:
        m._apply_options({"OPTIONS": {"log_dir": "/tmp/applied", "write_db": False}})
        assert m.LOG_DIR == "/tmp/applied"
        assert m.OPTIONS["write_db"] is False
    finally:
        m.LOG_DIR = old
        m._apply_options({})


# ------------------------------------------------------------------ 命令行覆盖
def _parse(argv):
    from daily_checkin import main as m

    return m.parse_arguments(argv)


def test_cli_has_db_arguments():
    args = _parse(["--db-path", "/tmp/x.db", "--no-db", "--log-dir", "/tmp/lg"])
    assert args.db_path == "/tmp/x.db"
    assert args.no_db is True
    assert args.log_dir == "/tmp/lg"


def test_cli_defaults_are_none():
    """不传时必须是 None/False，不能变成空串把配置冲掉。"""
    args = _parse([])
    assert args.db_path is None
    assert args.no_db is False
    assert args.log_dir is None


def test_cli_overrides_config():
    import daily_checkin.main as m
    from daily_checkin.utils import db

    old_log, old_cfg = m.LOG_DIR, m._CONFIG_DATA
    try:
        m._apply_options(
            {"OPTIONS": {"log_dir": "/tmp/from-cfg", "db_path": "/tmp/cfg.db"}}
        )
        assert m.LOG_DIR == "/tmp/from-cfg"
        assert db.db_path() == "/tmp/cfg.db"

        m._apply_cli_options(_parse(["--log-dir", "/tmp/from-cli", "--db-path", "/tmp/cli.db"]))
        assert m.LOG_DIR == "/tmp/from-cli", "命令行应覆盖配置"
        assert db.db_path() == "/tmp/cli.db"
    finally:
        m.LOG_DIR, m._CONFIG_DATA = old_log, old_cfg
        m._CLI_OVERRIDES.update({"log_dir": None, "db_path": None, "write_db": None})
        m._resolve_options()


def test_cli_no_db_disables_write():
    import daily_checkin.main as m

    old = dict(m._CLI_OVERRIDES)
    try:
        m._apply_cli_options(_parse(["--no-db"]))
        assert m.OPTIONS["write_db"] is False
        m._CONFIG_DATA = {}
        m._resolve_options()
        assert m._write_db("2026-10-20 10:00:00", [{"name": "A", "ok": True, "message": "x"}]) == (0, "")
    finally:
        m._CLI_OVERRIDES.update(old)
        m._CONFIG_DATA = {}
        m._resolve_options()


# ------------------------------------------------------------------ 查库旗标
def _run_db(argv):
    """模拟 `dailycheckin --db-...`，返回 (退出码, stdout+stderr)。

    错误信息走 stderr（比如写语句被拒），只抓 stdout 会漏掉。
    """
    import io as _io
    from contextlib import redirect_stderr, redirect_stdout

    from daily_checkin import main as m

    buf = _io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        rc = m.checkin(argv)
    return rc, buf.getvalue()


def test_db_flag_init():
    """--db-init 应建表，不跑签到。"""
    path = _tmp_db()
    rc, out = _run_db(["--db-path", path, "--db-init"])
    assert rc == 0
    assert "t_checkin_records" in out, "应看到建表输出"
    assert os.path.exists(path)


def test_db_flag_does_not_write_logs():
    """查库不该产生 logs/ 或 data/。"""
    path = _tmp_db()
    rc, _ = _run_db(["--db-path", path, "--db-init"])
    assert rc == 0
    assert not os.path.exists(os.path.join(_ROOT, "logs")), "查库不应写日志"


def test_db_flag_query_and_stats():
    path = _tmp_db()
    _run_db(["--db-path", path, "--db-init"])
    rc, out = _run_db(
        ["--db-path", path, "--db-sql",
         "INSERT INTO t_checkin_records(platform,platform_name,account_index,status,message,created_at)"
         " VALUES('V2EX','V2EX',0,'ok','签到成功','2026-10-20 15:00:00')",
         "--db-write"]
    )
    assert rc == 0, out
    rc, out = _run_db(["--db-path", path, "--db-query", "--db-platform", "V2EX"])
    assert rc == 0
    assert "签到成功" in out
    rc, out = _run_db(["--db-path", path, "--db-stats"])
    assert rc == 0
    assert "V2EX" in out


def test_db_flag_rejects_write_without_flag():
    path = _tmp_db()
    _run_db(["--db-path", path, "--db-init"])
    rc, out = _run_db(["--db-path", path, "--db-sql", "DELETE FROM t_checkin_records"])
    assert rc == 2, "写语句应被拒绝并返回 2"
    assert "只读" in out


def test_db_flag_conflict():
    """两个 --db-* 动作同时给应报错。"""
    from daily_checkin import main as m

    try:
        m._db_action(m.parse_arguments(["--db-stats", "--db-query"]))
    except SystemExit as exc:
        assert "只能选一个" in str(exc)
    else:
        raise AssertionError("应抛 SystemExit")


def test_db_flag_json_output():
    path = _tmp_db()
    _run_db(["--db-path", path, "--db-init"])
    _run_db(["--db-path", path, "--db-sql",
             "INSERT INTO t_checkin_records(platform,platform_name,account_index,status,message,created_at)"
             " VALUES('BING','Microsoft Rewards',0,'fail','失败','2026-10-20 15:00:00')",
             "--db-write"])
    rc, out = _run_db(["--db-path", path, "--db-query", "--db-json"])
    assert rc == 0
    assert '"platform": "BING"' in out



def _main():
    passed = failed = 0
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                func()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
        else:
            passed += 1
            print(f"PASS  {name}")
    print()
    print("全部通过" if not failed else f"{passed} 通过 / {failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
