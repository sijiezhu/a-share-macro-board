"""最小 .env 读取：只解析 KEY=VALUE，不执行任何 shell 语法。"""

from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: Path | str, *, override: bool = False) -> dict[str, str]:
    """读取环境变量文件并写入 os.environ，返回已加载的键值。"""
    env_path = Path(path)
    loaded: dict[str, str] = {}
    if not env_path.exists():
        return loaded
    mode = env_path.stat().st_mode & 0o777
    if mode & 0o007:
        raise PermissionError(f'{env_path} 权限过于宽松（{mode:o}），请改为 600')
    for raw_line in env_path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
        loaded[key] = value
    return loaded
