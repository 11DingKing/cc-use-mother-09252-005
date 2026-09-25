"""本地启动入口：``python3 -m service_09252_005``。

环境变量：
* ``TRAINING_DB_URL``：数据库连接串（默认 SQLite，位于系统临时目录）；
* ``TRAINING_DATA_DIR``：SQLite 数据目录；
* ``ADMIN_TOKEN``：启动时自动创建的管理员令牌。
"""
from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "service_09252_005.api.app:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8080")),
        reload=False,
    )


if __name__ == "__main__":
    main()
