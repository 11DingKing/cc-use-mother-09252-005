"""测试公共夹具：固定时钟、序列号 ID、临时数据库。"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import datetime, timezone

from service_09252_005 import create_app
from service_09252_005.ports import FixedClock, SequentialIds
from service_09252_005.privacy import Actor

ADMIN = Actor("admin", "root")
FIXED_NOW = datetime(2026, 1, 5, 8, 0, 0, tzinfo=timezone.utc)


def make_app(case, db_path=None):
    tmp = tempfile.mkdtemp(prefix="svc09252_")
    case.addCleanup(shutil.rmtree, tmp, True)
    app = create_app(
        db_path=db_path or os.path.join(tmp, "app.db"),
        clock=FixedClock(FIXED_NOW),
        ids=SequentialIds(),
    )
    case.addCleanup(app.close)
    return app


def seed_enterprise(app, eid="ent1", region="苏州"):
    return app.service.create_enterprise(
        ADMIN, {"id": eid, "name": f"企业{eid}", "region": region})


def seed_school(app, sid="sch1", region="苏州"):
    return app.service.create_school(
        ADMIN, {"id": sid, "name": f"院校{sid}", "region": region})


def seed_commitment(app, eid="ent1", quarter="2026Q1", capacity=2, skills=("weld",),
                    key="p1", region="苏州", idem=None):
    return app.service.import_commitment(ADMIN, {
        "enterprise_id": eid, "quarter": quarter,
        "positions": [{"position_key": key, "title": f"岗位{key}", "region": region,
                       "skills": list(skills), "capacity": capacity}]}, idem)


def seed_students(app, sid="sch1", count=3, region="苏州", skills=("weld",),
                  valid_until="2026-03-31", completed_at="2025-12-01",
                  prefix="stu", with_safety=True):
    students = []
    for i in range(count):
        s = {"id": f"{prefix}{i}", "name": f"学生{i}", "region": region,
             "skills": list(skills), "phone": f"1381234{i:04d}",
             "id_number": f"32050120060101{i:04d}"}
        if with_safety:
            s["safety"] = {"completed_at": completed_at, "valid_until": valid_until}
        students.append(s)
    app.service.import_students(ADMIN, {"school_id": sid, "students": students}, None)
    return [s["id"] for s in students]


def seed_demands(app, sid="sch1", quarter="2026Q1", region="苏州", skill="weld", count=3):
    return app.service.import_demands(ADMIN, {
        "school_id": sid, "quarter": quarter,
        "demands": [{"region": region, "skill": skill, "count": count}]}, None)


def position_id(app, commitment_id, index=0):
    return app.service.get_commitment(ADMIN, commitment_id)["positions"][index]["id"]


def allocations(app, status=None, quarter="2026Q1"):
    query = {}
    if quarter:
        query["quarter"] = quarter
    if status:
        query["status"] = status
    return app.service.list_allocations(ADMIN, query)["allocations"]


def insert_allocation(app, position_id, student_id, quarter, status,
                      school_id="sch1", region="苏州", rank=None):
    """测试夹具：绕过匹配直接落一条分配记录（构造竞争/历史场景）。"""
    with app.store.write_tx() as tx:
        aid = app.ids.new_id("alc")
        now = app.clock.now().isoformat()
        tx.execute(
            "INSERT INTO allocations (id, quarter, position_id, student_id, school_id,"
            " region, status, rank, reason, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (aid, quarter, position_id, student_id, school_id, region, status, rank, now, now))
        return aid
