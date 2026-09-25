"""运行期配置。

运行数据与本地配置不得写入源码目录，因此数据库默认放在系统临时目录下，
也可以通过环境变量 ``TRAINING_DATA_DIR`` / ``TRAINING_DB_URL`` 覆盖。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def data_dir() -> Path:
    root = os.environ.get("TRAINING_DATA_DIR")
    if root:
        path = Path(root)
    else:
        path = Path(tempfile.gettempdir()) / "training_booking_09252"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_db_url() -> str:
    override = os.environ.get("TRAINING_DB_URL")
    if override:
        return override
    return f"sqlite:///{data_dir() / 'app.db'}"
