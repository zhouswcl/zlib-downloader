#!/bin/bash
# Z-Library 每日下载器 - cron 入口
# 从 .env 加载凭据并执行 local_run.py

cd "$(dirname "$0")"

# 加载环境变量
set -a
source .env
set +a

# 执行下载
python3 local_run.py --json >> "logs/$(date +\%Y\%m\%d).log" 2>&1
