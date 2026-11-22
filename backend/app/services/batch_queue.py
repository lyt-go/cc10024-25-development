"""批次队列领域服务：采集任务批次管理，消费script_schedule调度命令，写入run_history运行记录。"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services.base import BaseModuleService, register_hook
from app import persistence as persist


def _complete_batch_to_history(entry: dict[str, Any]) -> None:
    """批次确认完成时写入run_history，并带上派发时的清单来源和页面范围。"""
    run_rows = store.rows("run_history")
    new_id = max((int(r.get("id", 0)) for r in run_rows), default=0) + 1
    pages = str(entry.get("页面范围", "") or "")
    page_count = len([p for p in pages.replace("，", ",").split(",") if p.strip()])
    record = {
        "id": new_id,
        "记录编号": f"RUN-BATCH-{new_id:04d}",
        "关联计划": entry.get("关联计划", ""),
        "开始时间": persist.now_iso(),
        "结束时间": persist.now_iso(),
        "采集行数": str(page_count or entry.get("任务数量", 0)),
        "失败行数": str(entry.get("失败数", 0)),
        "执行日志": f"批次{entry.get('批次编号')}执行完成",
        "异常摘要": "",
        "status": "已完成",
        "pending": False,
        "abnormal": False,
        "version": "",
        "updated_at": persist.now_iso(),
        # 采集与结算的数据范围：批次实际采集的页面及其来源清单。
        "来源批次": entry.get("批次编号", ""),
        "来源清单": entry.get("来源清单", ""),
        "预演页数": int(entry.get("预演页数", entry.get("任务数量", 0)) or 0),
        "采集页数": page_count or int(entry.get("任务数量", 0) or 0),
        "页面范围": pages,
    }
    run_rows.append(record)
    persist.append_jsonl("runs", "run_history.jsonl", {
        "module": "run_history", "entity": "运行记录", "entry_id": new_id,
        "status": "已完成", "version": "", "action": "auto_create_from_batch",
        "timestamp": persist.now_iso(), "abnormal": False,
        "source_module": "batch_queue", "source_id": entry.get("id"),
    })

register_hook("batch_queue", "确认完成", _complete_batch_to_history)


class BatchQueueService(BaseModuleService):
    MODULE = "batch_queue"
    ENTITY = "采集批次"
    OPERATION_TERM = "调度"
    LIST_FIELDS = ["批次编号", "关联计划", "来源清单", "页面范围", "预演页数", "实际页数", "任务数量", "批次状态"]
    REQUIRED_FIELDS = ["批次编号", "关联计划", "任务数量"]
    STATUS_ORDER = ["排队中", "执行中", "已完成", "已超时"]
    ACTION_RULES = {"发起执行": "执行中", "确认完成": "已完成", "标记超时": "已超时"}
    NEGATIVE_ACTIONS = []

    def list_entries(self, **kwargs):
        """批次列表把清单派发字段一并带出，便于值班人核对来源清单和页面范围。"""
        rows, total = super().list_entries(**kwargs)
        return [self._with_manifest_fields(row) for row in rows], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = super().get_entry(entry_id)
        return self._with_manifest_fields(row) if row is not None else None

    @staticmethod
    def _with_manifest_fields(row: dict[str, Any]) -> dict[str, Any]:
        """补齐清单派发字段；返回副本，避免列表查询污染仓储记录。"""
        view = dict(row)
        pages = str(view.get("页面范围", "") or "")
        view["来源清单"] = view.get("来源清单", "")
        view["预演页数"] = view.get("预演页数", view.get("任务数量", 0))
        view["实际页数"] = view.get(
            "实际页数",
            len([p for p in pages.replace("，", ",").split(",") if p.strip()]),
        )
        view["页面范围"] = pages
        return view

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        batches = store.rows(self.MODULE)
        total_tasks = sum(int(r.get("任务数量", 0)) for r in batches)
        base["total_tasks"] = total_tasks
        # 关联的run_history
        runs = persist.read_jsonl("runs", "run_history.jsonl")
        batch_runs = [r for r in runs if "batch" in str(r.get("action", ""))]
        base["batch_runs"] = len(batch_runs)
        # 关联的error_recover
        errors = store.rows("error_recover")
        batch_plans = {r.get("关联计划") for r in batches}
        base["recovery_linked"] = sum(1 for e in errors if e.get("关联计划") in batch_plans)
        return base