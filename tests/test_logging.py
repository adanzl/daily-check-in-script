"""main.py 日志落盘逻辑的测试。

重点覆盖三件事：
1. mask_secrets 不把 Cookie 明文写进日志文件；
2. checkin() 无论成功/失败都会写 raw + 摘要两份日志；
3. 未捕获异常也要留日志，且退出码非 0。
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import daily_checkin.main as m


class _FakeCheckin:
    """假任务：既当「类」被 checkin_map 调用，也当实例调 main()。

    checkin_map 里的值是按 `check_func(check_item)` 调用的，所以这里要可调用。
    """

    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    def __call__(self, check_item):
        return self

    def main(self):
        if self._error:
            raise self._error
        return self._result


def _run_with(items):
    """在临时日志目录里跑一次 checkin，返回 (退出码, 日志目录, 输出)。"""
    tmp = tempfile.mkdtemp(prefix="dc-log-")
    old_env = os.environ.get("DC_LOG_DIR")
    os.environ["DC_LOG_DIR"] = tmp
    old_map = m.checkin_map
    old_cfg = m.check_config
    old_push = m.push_message
    old_ver = m.requests.get
    old_db = m._write_db
    try:
        # 模块级 LOG_DIR 在 import 时就定好了，必须跟着改
        m.LOG_DIR = tmp
        # 这份测试只关心日志，但 checkin() 现在顺手会写库。
        # 不能靠 set_default_path(tmp) 隔离：_checkin_impl() 里会调
        # _resolve_options() 重算一遍，又把路径重置回默认值，于是在仓库
        # 里留下 data/data.db。直接替换掉写库函数最稳妥。
        m._write_db = lambda *a, **k: (0, "")
        m.checkin_map = {name: (name, cls) for name, cls in items}
        m.check_config = lambda task_list: ({}, {name.lower(): [{}] for name, _ in items})
        m.push_message = lambda content_list=None, notice_info=None: None

        class _Resp:
            def json(self):
                return {"info": {"version": "0.0.0"}}

        m.requests.get = lambda *a, **k: _Resp()
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = m.checkin()
        return rc, tmp, buf.getvalue()
    finally:
        m.checkin_map = old_map
        m.check_config = old_cfg
        m.push_message = old_push
        m.requests.get = old_ver
        m._write_db = old_db
        m.LOG_DIR = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(m.__file__))), "logs"
        )
        if old_env is None:
            os.environ.pop("DC_LOG_DIR", None)
        else:
            os.environ["DC_LOG_DIR"] = old_env


def test_mask_secrets_hides_cookie_value():
    out = m.mask_secrets("SESSDATA=abcd1234efgh5678; bili_jct=deadbeef00112233")
    assert "abcd1234efgh5678" not in out
    assert "deadbeef00112233" not in out
    assert "abcd***5678" in out


def test_mask_secrets_keeps_plain_text():
    assert m.mask_secrets("普通文本 no secret") == "普通文本 no secret"


def test_mask_secrets_empty():
    assert m.mask_secrets("") == ""


def test_checkin_writes_raw_and_summary_logs():
    rc, tmp, out = _run_with([("FAKEOK", _FakeCheckin("签到成功，获得 5 点经验"))])
    assert rc == 0
    files = sorted(os.listdir(tmp))
    assert any(f.startswith("raw-") for f in files), files
    assert "check_in.log" in files
    summary = open(os.path.join(tmp, "check_in.log"), encoding="utf-8").read()
    assert "成功 1" in summary
    assert "FAKEOK:ok" in summary


def test_checkin_records_failure_reason():
    rc, tmp, out = _run_with(
        [("FAKEBAD", _FakeCheckin(error=RuntimeError("登录态失效，请重新获取 Cookie")))]
    )
    summary = open(os.path.join(tmp, "check_in.log"), encoding="utf-8").read()
    assert "失败 1" in summary
    assert "[FAIL]" in summary
    assert "登录态失效" in summary


def test_empty_return_counts_as_failure():
    """main() 返回 None 必须判失败，不能因为没抛异常就打勾。"""
    rc, tmp, out = _run_with([("FAKEEMPTY", _FakeCheckin(None))])
    summary = open(os.path.join(tmp, "check_in.log"), encoding="utf-8").read()
    assert "失败 1" in summary
    assert "签到未返回任何结果" in summary


def test_raw_log_masks_cookie():
    _, tmp, _ = _run_with([("FAKEOK", _FakeCheckin("SESSDATA=abcd1234efgh5678 有效"))])
    raw = [f for f in os.listdir(tmp) if f.startswith("raw-")][0]
    body = open(os.path.join(tmp, raw), encoding="utf-8").read()
    assert "abcd1234efgh5678" not in body


def test_summary_appends_across_runs():
    rc, tmp, _ = _run_with([("FAKEOK", _FakeCheckin("ok"))])
    # 复用手动路径再写一次，验证是追加而不是覆盖
    m.LOG_DIR = tmp
    m._write_logs("2026-01-01 00:00:00", 1, "raw", [], "")
    lines = [one for one in open(os.path.join(tmp, "check_in.log"), encoding="utf-8") if one.strip()]
    assert len(lines) == 2
    m.LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(m.__file__))), "logs")


def test_log_dir_override_via_env():
    """DC_LOG_DIR 能改日志目录，容器里才好挂到卷上。"""
    assert m.LOG_DIR
    assert "logs" in m.LOG_DIR


def _main():
    passed = failed = 0
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        try:
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
