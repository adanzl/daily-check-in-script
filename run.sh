#!/usr/bin/env bash
# 用项目内 .venv 启动签到。没有虚拟环境时创建并安装依赖。
#
# 参数原样传给 daily_checkin.main：
#   --include TASK [TASK...]   只执行这些任务
#   --exclude TASK [TASK...]   排除这些任务
#   --db PATH                  签到记录库路径（覆盖 OPTIONS.db_path / DC_DB）
#   --no-db                    本次不写签到记录库
#   --log-dir DIR              运行日志目录（覆盖 OPTIONS.log_dir / DC_LOG_DIR）
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
