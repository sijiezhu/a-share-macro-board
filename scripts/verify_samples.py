#!/usr/bin/env python3
"""抽查：从本地库取若干历史日期，独立重取来源数据核对利差计算。

用法：
    python scripts/verify_samples.py                          # 首/中/末三个共同日期
    python scripts/verify_samples.py --dates 2024-01-15,2025-09-18
    python scripts/verify_samples.py --tol 1e-6
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from macroboard import db, indicators  # noqa: E402
from macroboard.config import DEFAULT_DB_PATH, DEFAULT_ENV_FILE  # noqa: E402
from macroboard.env import load_env_file  # noqa: E402
from macroboard.fetch import new_session  # noqa: E402
from macroboard.sources import cn_bond, mx  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='抽查股债利差历史样本')
    parser.add_argument('--db', type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument('--env-file', type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument('--dates', type=str, default='', help='逗号分隔的 YYYY-MM-DD，默认取首/中/末')
    parser.add_argument('--tol', type=float, default=1e-6, help='允许的数值偏差')
    return parser.parse_args(argv)


def pick_dates(conn, explicit: str) -> list[date]:
    if explicit:
        return [date.fromisoformat(item.strip()) for item in explicit.split(',') if item.strip()]
    pe = {row['obs_date']: row['value'] for row in db.observations_all(conn, 'hs300_pe_ttm')}
    bond = {row['obs_date']: row['value'] for row in db.observations_all(conn, 'cn10y')}
    common = sorted(set(pe) & set(bond))
    return [date.fromisoformat(common[0]), date.fromisoformat(common[len(common) // 2]), date.fromisoformat(common[-1])]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_env_file(args.env_file)
    conn = db.connect(args.db)
    dates = pick_dates(conn, args.dates)
    pe_map = {row['obs_date']: row['value'] for row in db.observations_all(conn, 'hs300_pe_ttm')}
    bond_map = {row['obs_date']: row['value'] for row in db.observations_all(conn, 'cn10y')}
    conn.close()

    session = new_session()
    api_key = mx.api_key_from_env()
    if not api_key:
        print('[失败] 缺少 MX_APIKEY，无法独立核对 PE')
        return 1

    pe_cache: dict[int, dict[str, float]] = {}
    failures = 0
    for day in dates:
        key = day.isoformat()
        if day.year not in pe_cache:
            fetched = mx.fetch_hs300_pe(session, api_key, date(day.year, 1, 1), date(day.year, 12, 31))
            pe_cache[day.year] = {d.isoformat(): v for d, v in fetched.rows}
        ref_pe = pe_cache[day.year].get(key)
        ref_bond_rows = cn_bond.fetch(session, day, day)
        ref_bond = dict((d.isoformat(), v) for d, v in ref_bond_rows).get(key)

        db_pe = pe_map.get(key)
        db_bond = bond_map.get(key)
        expected = indicators.equity_bond_spread(db_pe, db_bond)
        recomputed = indicators.equity_bond_spread(ref_pe, ref_bond)
        ok = (
            ref_pe is not None
            and ref_bond is not None
            and db_pe == ref_pe
            and db_bond == ref_bond
            and expected is not None
            and recomputed is not None
            and abs(expected - recomputed) <= args.tol
        )
        failures += 0 if ok else 1
        print(
            f"[{'通过' if ok else '不一致'}] {key} "
            f"PE 库={db_pe} 源={ref_pe} | 中债10Y 库={db_bond} 源={ref_bond} | "
            f"利差={indicators.to_percentage_points(expected):.4f} 个百分点 "
            f"({indicators.to_bp(expected):.2f} BP) 复核={indicators.to_percentage_points(recomputed):.4f}"
        )
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
