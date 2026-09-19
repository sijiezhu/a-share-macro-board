#!/usr/bin/env python3
"""数据采集入口：把各指标写入本地 SQLite（幂等，可重复执行）。

用法：
    python update_data.py                # 增量更新
    python update_data.py --full         # 首次运行，补齐股债利差所需历史
    python update_data.py --only cn10y --only hs300_pe_ttm
    python update_data.py --db data/macroboard.sqlite3

退出码：0 = 全部成功；1 = 有指标更新失败（页面仍会显示上次成功值）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from macroboard.collect import collect
from macroboard.config import DEFAULT_DB_PATH, DEFAULT_ENV_FILE, SERIES, STATUS_LABELS
from macroboard.env import load_env_file


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='采集 A股投资信息看板 所需数据')
    parser.add_argument('--db', type=Path, default=DEFAULT_DB_PATH, help='SQLite 数据库路径')
    parser.add_argument('--env-file', type=Path, default=DEFAULT_ENV_FILE, help='可选的环境变量文件')
    parser.add_argument('--full', action='store_true', help='补齐股债利差所需的历史数据')
    parser.add_argument('--only', action='append', choices=sorted(SERIES), help='只采集指定指标')
    parser.add_argument('--quiet', action='store_true', help='只输出一行总结')
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        load_env_file(args.env_file)
    except PermissionError as exc:
        print(f'[警告] {exc}', file=sys.stderr)

    results = collect(args.db, full=args.full, only=args.only)

    if not args.quiet:
        print(f'数据文件：{args.db}')
        for item in results:
            mark = 'OK ' if item.ok else 'ERR'
            status = STATUS_LABELS.get(item.status, item.status)
            detail = f' · {item.message}' if item.message else ''
            print(f'  [{mark}] {item.label}：{status}（本次抓取 {item.rows} 条）{detail}')

    failed = [item for item in results if not item.ok]
    print(f'采集完成：成功 {len(results) - len(failed)}/{len(results)}，失败 {len(failed)}')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
