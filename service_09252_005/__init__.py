"""产教融合实训岗位承诺的服务端包入口。"""
from .app import App, create_app

PROJECT_CODE = "service_09252_005"


def project_info() -> dict[str, str]:
    """返回稳定的项目标识。"""
    return {"code": PROJECT_CODE, "title": "产教融合实训岗位承诺"}


__all__ = ["App", "create_app", "PROJECT_CODE", "project_info"]
