"""情景比较与结果发布。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..schemas_api import CompareRequest, PublishRequest
from ..services.comparison import build_comparison, publish_comparison
from .deps import get_db, jsonable

router = APIRouter(tags=["比较与发布"])


@router.post("/comparisons")
def compare(payload: CompareRequest, conn=Depends(get_db)):
    """以共同分母比较两个任务：道路效应、人口修订、边界变化分别展示。"""
    try:
        result = build_comparison(conn, payload.task_a_id, payload.task_b_id)
    except TypeError as exc:
        raise HTTPException(404, f"任务不存在或上下文不完整：{exc}")
    return jsonable(result)


@router.post("/publications")
def publish(payload: PublishRequest, conn=Depends(get_db)):
    """发布比较：完整输入清单（修订号/截止点/指纹）随结果落库。"""
    result = publish_comparison(
        conn, payload.task_a_id, payload.task_b_id, payload.title, payload.created_by
    )
    return jsonable(result)


@router.get("/publications")
def list_publications(conn=Depends(get_db)):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT publication_id, title, scenario_a_id, scenario_b_id, "
            "evidence_cutoff_at, created_by, created_at, input_manifest "
            "FROM publications ORDER BY created_at DESC"
        )
        rows = cur.fetchall()
    return jsonable({"publications": rows})


@router.get("/publications/{publication_id}")
def get_publication(publication_id: str, conn=Depends(get_db)):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM publications WHERE publication_id=%s", (publication_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "发布记录不存在")
    return jsonable(row)
