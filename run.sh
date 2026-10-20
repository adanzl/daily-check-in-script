#!/usr/bin/env bash
# 用项目内 .venv 启动签到。没有虚拟环境时创建并安装依赖。
#
# 参数原样传给 daily_checkin.main，与 parse_arguments 保持一致：
#   --include TASK [TASK...]   只执行这些任务
#   --exclude TASK [TASK...]   排除这些任务
#   --db-path PATH             签到记录库路径（覆盖 OPTIONS.db_path / DC_DB）
#   --no-db                    本次不写签到记录库
#   --log-dir DIR              运行日志目录（覆盖 OPTIONS.log_dir / DC_LOG_DIR）
#   --db-init                  建表（幂等）
#   --db-paths                 显示库位置
#   --db-stats                 各平台成功/失败汇总
#   --db-query                 查询签到记录
#   --db-sql SQL               执行 SQL；默认只读，要写加 --db-write
#   --db-write                 配合 --db-sql 允许写语句
#   --db-platform NAME         筛选平台，如 BILIBILI
#   --db-status ok|fail        筛选状态
#   --db-since TIME            created_at ≥ 该值，如 2026-10-19
#   --db-limit N               查询条数上限（默认 20）
#   --db-json                  以 JSON 输出
# 给了 --db-init / --db-paths / --db-stats / --db-query / --db-sql 之一就只查库，不跑签到。
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -x .venv/bin/python ]]; then
  if ! command -v python3 >/dev/null 2>&1; then
    echo "未找到 python3，无法创建 .venv" >&2
    exit 1
  fi
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
fi

exec .venv/bin/python -m daily_checkin.main "$@"
