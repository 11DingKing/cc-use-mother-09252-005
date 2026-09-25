"""服务入口：python3 -m service_09252_005 [--host H] [--port P] [--db PATH]"""
from __future__ import annotations

import argparse

from .app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="产教融合实训岗位承诺服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default=None, help="SQLite 数据库路径（默认位于系统临时目录）")
    args = parser.parse_args()
    app = create_app(db_path=args.db)
    if app.recovered_jobs:
        print(f"已恢复 {len(app.recovered_jobs)} 个未完成补偿作业: {app.recovered_jobs}")
    print(f"listening on http://{args.host}:{args.port} (db: {app.db_path})")
    try:
        app.serve(args.host, args.port)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
