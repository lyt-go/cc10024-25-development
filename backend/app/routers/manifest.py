"""定时采集执行清单专用接口：预演视图、页面追溯和从脚本输入生成清单。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.schemas import ActionResult
from app.services import service_for

router = APIRouter(prefix="/api/collection_manifest", tags=["定时采集执行清单"])


class FromScriptPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


def _service():
    service = service_for("collection_manifest")
    if service is None:  # pragma: no cover - 注册表固定包含该模块
        raise HTTPException(status_code=404, detail="未知业务模块：collection_manifest")
    return service


# 固定路径必须放在 /{module}/{entry_id} 之前注册，否则会被动态路径抢先匹配。
@router.get("/preview")
def preview_all() -> dict[str, Any]:
    """预演视图：全部清单的种子页范围、重叠关系、页面归属与批次历史。"""
    return _service().preview_all()


@router.get("/trace")
def page_trace() -> list[dict[str, Any]]:
    """页面追溯：每个页面由哪次调用采到、下一批还能领哪些页面。"""
    return _service().page_trace()


@router.get("/preview/{entry_id}")
def preview(entry_id: int) -> dict[str, Any]:
    """单个清单的预演报告。"""
    try:
        return _service().preview(entry_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"定时采集执行清单 {entry_id} 不存在或已归档")


@router.post("/from-script", response_model=ActionResult)
def create_from_script(payload: FromScriptPayload) -> ActionResult:
    """从脚本输入（调度计划 + 种子页）生成定时采集执行清单。"""
    entry, missing = _service().create_from_script(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="定时采集执行清单已生成", entry=entry)
