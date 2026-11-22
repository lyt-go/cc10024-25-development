"""定时采集执行清单领域服务。

职责链路：

1. 从脚本输入（调度计划 + 种子页）生成执行清单，清单只记录“预演时看到的种子页范围”；
2. 预演视图列出清单之间的重叠关系、每个页面的当前状态，以及下一批还能领取哪些页面；
3. 派发批次时按清单重新判定页面归属，已经开始采集（排队中/执行中）或已经处理
   （已完成）的页面不会再被第二个批次派发；
4. 批次记录派发时的来源清单与预演页数，实际派出的页面写入页面台账；
5. 批次确认完成时，把实际采集到的页面带入所有覆盖这些页面的清单结算，重叠页的
   来源仍然指向真正采到它的那个批次/清单。
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.store import store
from app.services.base import BaseModuleService, register_hook, _version_id
from app import persistence as persist

MANIFEST_MODULE = "collection_manifest"
LEDGER_MODULE = "_collection_page_ledger"
BATCH_MODULE = "batch_queue"
RUN_MODULE = "run_history"

# 种子页分隔符：中英文逗号、顿号、分号、竖线、斜杠、空白都可以。
_PAGE_SPLIT = re.compile(r"[,，、;；|/\s]+")
_ACTIVE_STATUS = ("排队中", "执行中")
_CJK_RANGE = ("一", "鿿")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _now() -> str:
    return persist.now_iso()


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def parse_pages(value: Any) -> list[str]:
    """把脚本输入 / 表单里的种子页解析成有序、去重的页面列表。

    - 列表逐项展开；
    - 字符串按中英文逗号、顿号、分号、斜杠、空白切分；
    - 没有分隔符且全部是中文时按单字切分，因此“甲乙”等价于“甲、乙”。
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        result: list[str] = []
        for item in value:
            result.extend(parse_pages(item))
        return _dedupe(result)
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        try:
            return parse_pages(json.loads(text))
        except (TypeError, ValueError):
            pass
    parts = [part.strip() for part in _PAGE_SPLIT.split(text) if part.strip()]
    if len(parts) == 1 and len(parts[0]) > 1:
        token = parts[0]
        if all(_CJK_RANGE[0] <= char <= _CJK_RANGE[1] for char in token):
            parts = list(token)
    return _dedupe(parts)


def _manifest_pages(entry: dict[str, Any]) -> list[str]:
    return parse_pages(entry.get("种子页范围"))


def _manifest_code(entry: dict[str, Any]) -> str:
    return _text(entry.get("清单编号"))


def _batch_pages(batch: dict[str, Any]) -> list[str]:
    return parse_pages(batch.get("页面范围"))


# ---------------------------------------------------------------------------
# 页面台账：记录每个页面被哪些批次领取、是否已处理、由哪次调用采到
# ---------------------------------------------------------------------------

def _ledger_rows() -> list[dict[str, Any]]:
    return store.rows(LEDGER_MODULE)


def _ledger_row(page: str, create: bool = False) -> dict[str, Any] | None:
    for row in _ledger_rows():
        if row.get("页面") == page:
            return row
    if not create:
        return None
    row = {
        "id": max((_as_int(r.get("id")) for r in _ledger_rows()), default=0) + 1,
        "页面": page,
        "状态": "可领",
        "批次记录": [],
        "覆盖清单": [],
        "结算批次": "",
        "结算时间": "",
        "运行记录": "",
        "updated_at": _now(),
    }
    _ledger_rows().append(row)
    return row


def _batch_by_record(record: dict[str, Any]) -> dict[str, Any] | None:
    batch_id = _as_int(record.get("批次ID"))
    if batch_id:
        return store.find(BATCH_MODULE, batch_id)
    code = _text(record.get("批次编号"))
    for batch in store.rows(BATCH_MODULE):
        if _text(batch.get("批次编号")) == code:
            return batch
    return None


def page_status(page: str) -> str:
    """页面在当前时刻的可派发状态：可领 / 已派发 / 已处理。"""
    row = _ledger_row(page)
    if row is None:
        return "可领"
    statuses: list[str] = []
    for record in row.get("批次记录", []):
        batch = _batch_by_record(record)
        statuses.append(_text(batch.get("status")) if batch else _text(record.get("批次状态")))
    if "已完成" in statuses:
        return "已处理"
    if any(status in _ACTIVE_STATUS for status in statuses):
        return "已派发"
    # 全部是已超时等非活动状态时，页面重新回到可领。
    return "可领"


def _claim_page(page: str, batch: dict[str, Any]) -> None:
    row = _ledger_row(page, create=True)
    assert row is not None
    row.setdefault("批次记录", []).append({
        "批次编号": _text(batch.get("批次编号")),
        "批次ID": batch.get("id"),
        "清单编号": _text(batch.get("来源清单")),
        "派发时间": batch.get("派发时间") or _now(),
        "批次状态": _text(batch.get("status")),
    })
    row["状态"] = "已派发"
    row["updated_at"] = _now()


def _mark_page_settled(page: str, batch: dict[str, Any], run: dict[str, Any] | None) -> None:
    row = _ledger_row(page, create=True)
    assert row is not None
    row["状态"] = "已处理"
    row["结算批次"] = _text(batch.get("批次编号"))
    row["结算时间"] = _now()
    row["运行记录"] = _text((run or {}).get("记录编号"))
    row["updated_at"] = _now()


def _refresh_coverage() -> None:
    manifests = list(store.rows(MANIFEST_MODULE))
    for manifest in manifests:
        for page in _manifest_pages(manifest):
            _ledger_row(page, create=True)
    for row in _ledger_rows():
        row["覆盖清单"] = [
            _manifest_code(manifest)
            for manifest in manifests
            if row.get("页面") in _manifest_pages(manifest)
        ]


def _save_ledger() -> None:
    persist.write_json("runs", "collection_page_ledger.json", {
        "updated_at": _now(),
        "pages": _ledger_rows(),
    })


def _run_for_batch(batch_code: str) -> dict[str, Any] | None:
    if not batch_code:
        return None
    matches = [
        row for row in store.rows(RUN_MODULE)
        if _text(row.get("来源批次")) == batch_code
    ]
    return matches[-1] if matches else None


# ---------------------------------------------------------------------------
# 清单字段/页数维护
# ---------------------------------------------------------------------------

def _refresh_counts(entry: dict[str, Any]) -> None:
    pages = _manifest_pages(entry)
    code = _manifest_code(entry)
    dispatched: set[str] = set()
    for batch in store.rows(BATCH_MODULE):
        if _text(batch.get("来源清单")) == code:
            dispatched.update(_batch_pages(batch))
    entry["预演页数"] = len(pages)
    entry["实际派发页"] = len([page for page in pages if page in dispatched])
    entry["待领页"] = sum(1 for page in pages if page_status(page) == "可领")
    entry["已结算页"] = sum(1 for page in pages if page_status(page) == "已处理")
    entry["updated_at"] = _now()


def _record_settlement(manifest: dict[str, Any], page: str, batch: dict[str, Any],
                       run: dict[str, Any] | None, source_code: str) -> bool:
    """把一个已处理页面带入清单结算；返回是否新写入。"""
    details = manifest.setdefault("结算明细", [])
    batch_code = _text(batch.get("批次编号"))
    for detail in details:
        if detail.get("页面") == page and detail.get("来源批次") == batch_code:
            return False
    detail = {
        "页面": page,
        "来源批次": batch_code,
        "来源清单": source_code,
        "页面来源": "本清单派发" if _manifest_code(manifest) == source_code else "重叠带入",
        "关联计划": _text(manifest.get("关联计划")),
        "结算时间": _now(),
        "运行记录": _text((run or {}).get("记录编号")),
        "运行记录ID": (run or {}).get("id"),
    }
    details.append(detail)
    persist.append_jsonl("reports", "manifest_settlement.jsonl", {
        "module": MANIFEST_MODULE,
        "action": "settle_page",
        "清单编号": _manifest_code(manifest),
        **detail,
    })
    return True


def _settle_batch_pages(entry: dict[str, Any]) -> None:
    """批次确认完成：页面入账，并带入所有覆盖页面的清单结算。"""
    pages = _batch_pages(entry)
    if not pages:
        return
    batch_code = _text(entry.get("批次编号"))
    source_code = _text(entry.get("来源清单"))
    run = _run_for_batch(batch_code)
    for page in pages:
        _mark_page_settled(page, entry, run)
        for manifest in list(store.rows(MANIFEST_MODULE)):
            if page in _manifest_pages(manifest):
                _record_settlement(manifest, page, entry, run, source_code)
    _refresh_coverage()
    for manifest in store.rows(MANIFEST_MODULE):
        _refresh_counts(manifest)
    _save_ledger()


def sync_batch_status(entry: dict[str, Any]) -> None:
    """批次状态流转后同步页面台账；完成即结算，超时则释放未处理的页面。"""
    batch_code = _text(entry.get("批次编号"))
    status = _text(entry.get("status")) or _text(entry.get("批次状态"))
    for row in _ledger_rows():
        touched = False
        for record in row.get("批次记录", []):
            if _text(record.get("批次编号")) == batch_code:
                record["批次状态"] = status
                touched = True
        if not touched:
            continue
        if status == "已完成":
            row["状态"] = "已处理"
        elif status in _ACTIVE_STATUS:
            if row.get("状态") != "已处理":
                row["状态"] = "已派发"
        elif row.get("状态") != "已处理":
            row["状态"] = "可领"
        row["updated_at"] = _now()
    if status == "已完成":
        _settle_batch_pages(entry)
    else:
        _refresh_coverage()
        for manifest in store.rows(MANIFEST_MODULE):
            _refresh_counts(manifest)
        _save_ledger()


for _action in ("发起执行", "确认完成", "标记超时"):
    register_hook("batch_queue", _action, sync_batch_status)


# ---------------------------------------------------------------------------
# 领域服务
# ---------------------------------------------------------------------------

class CollectionManifestService(BaseModuleService):
    MODULE = MANIFEST_MODULE
    ENTITY = "定时采集执行清单"
    OPERATION_TERM = "调度"
    LIST_FIELDS = ["清单编号", "关联计划", "关联脚本", "种子页范围", "预演页数", "实际派发页", "待领页", "清单状态"]
    REQUIRED_FIELDS = ["清单编号", "关联计划", "种子页范围"]
    STATUS_ORDER = ["草稿", "已预演", "已派发", "已结算"]
    ACTION_RULES = {"生成预演": "已预演", "派发批次": "已派发", "确认结算": "已结算"}
    NEGATIVE_ACTIONS = []

    # -- 登记 ---------------------------------------------------------------

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        raw_pages = values.get("种子页范围")
        if _text(raw_pages) and not parse_pages(raw_pages):
            return None, ["种子页范围"]
        values = {**values, "清单编号": self._unique_code(_text(values.get("清单编号")))}
        entry, missing = super().create_entry(values)
        if entry is None:
            return entry, missing
        entry["种子页范围"] = raw_pages if isinstance(raw_pages, str) else ",".join(parse_pages(raw_pages))
        entry["预览"] = None
        entry["结算明细"] = []
        entry["已结算页"] = 0
        entry["实际派发页"] = 0
        entry["预演页数"] = len(_manifest_pages(entry))
        entry["待领页"] = len(_manifest_pages(entry))
        entry["version"] = _version_id(entry)
        _refresh_coverage()
        _refresh_counts(entry)
        _save_ledger()
        return entry, []

    def _next_code(self) -> str:
        return self._unique_code("")

    def _unique_code(self, requested: str) -> str:
        """清单编号留空或重复时，按顺序补一个不冲突的编号。"""
        rows = store.rows(MANIFEST_MODULE)
        existing = {_text(row.get("清单编号")) for row in rows}
        candidate = _text(requested)
        if candidate and candidate not in existing:
            return candidate
        new_id = max((_as_int(row.get("id")) for row in rows), default=0) + 1
        code = f"MANI-{new_id:04d}"
        while code in existing:
            new_id += 1
            code = f"MANI-{new_id:04d}"
        return code

    def create_from_script(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """从脚本输入生成执行清单：接受计划编号/关联计划 + 种子页。"""
        plan_code = _text(
            values.get("关联计划") or values.get("计划编号") or values.get("plan_code")
        )
        raw_pages = (
            values.get("种子页范围") or values.get("种子页") or values.get("pages")
            or values.get("page_list") or values.get("页面")
        )
        pages = parse_pages(raw_pages)
        missing: list[str] = []
        if not plan_code:
            missing.append("关联计划")
        if not pages:
            missing.append("种子页范围")
        if missing:
            return None, missing

        plan = next(
            (row for row in store.rows("script_schedule")
             if _text(row.get("计划编号")) == plan_code),
            None,
        )
        script = _text(
            values.get("关联脚本") or values.get("脚本编号")
            or (plan.get("关联脚本") if plan else "")
        )
        record = {
            "清单编号": _text(values.get("清单编号")) or self._next_code(),
            "关联计划": plan_code,
            "关联脚本": script,
            "种子页范围": raw_pages if isinstance(raw_pages, str) else ",".join(pages),
        }
        return self.create_entry(record)

    # -- 动作 ---------------------------------------------------------------

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = self.get_entry(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action == "生成预演":
            report = self.build_preview(entry)
            self._record_preview(entry, report)
            _refresh_counts(entry)
            result, message = super().run_action(entry_id, action)
            return result, f"清单{_manifest_code(entry)}预演完成：{len(report['pages'])}页，可领{len(report['pending_pages'])}页"

        if action == "派发批次":
            ok, message = self.dispatch_batch(entry_id)
            if not ok:
                return None, message
            result, _ = super().run_action(entry_id, action)
            return result, message

        if action == "确认结算":
            ok, message = self.settle_manifest(entry_id)
            if not ok:
                return None, message
            result, _ = super().run_action(entry_id, action)
            return result, message

        return super().run_action(entry_id, action)

    # -- 预演 ---------------------------------------------------------------

    def build_preview(self, entry: dict[str, Any]) -> dict[str, Any]:
        pages = _manifest_pages(entry)
        page_details = []
        for page in pages:
            trace = self.page_trace_for(page)
            page_details.append({
                "页面": page,
                "状态": page_status(page),
                "采集批次": trace.get("采集批次", ""),
                "来源清单": trace.get("来源清单", ""),
                "结算时间": trace.get("结算时间", ""),
            })

        overlaps = []
        for other in store.rows(MANIFEST_MODULE):
            if other.get("id") == entry.get("id"):
                continue
            other_pages = _manifest_pages(other)
            shared = [page for page in pages if page in set(other_pages)]
            if shared:
                overlaps.append({
                    "清单编号": _manifest_code(other),
                    "关联计划": _text(other.get("关联计划")),
                    "重叠页": shared,
                    "重叠数": len(shared),
                })

        own_batches = [
            batch for batch in store.rows(BATCH_MODULE)
            if _text(batch.get("来源清单")) == _manifest_code(entry)
        ]
        return {
            "id": entry.get("id"),
            "清单编号": _manifest_code(entry),
            "关联计划": _text(entry.get("关联计划")),
            "关联脚本": _text(entry.get("关联脚本")),
            "种子页范围": _text(entry.get("种子页范围")),
            "清单状态": entry.get("status"),
            "预演页数": len(pages),
            "实际派发页": _as_int(entry.get("实际派发页")),
            "待领页": sum(1 for page in pages if page_status(page) == "可领"),
            "pages": page_details,
            "pending_pages": [detail["页面"] for detail in page_details if detail["状态"] == "可领"],
            "claimed_pages": [detail["页面"] for detail in page_details if detail["状态"] == "已派发"],
            "settled_pages": [detail["页面"] for detail in page_details if detail["状态"] == "已处理"],
            "overlaps": overlaps,
            "settlement": list(entry.get("结算明细", [])),
            "batches": [self._batch_view(batch) for batch in own_batches],
        }

    def _record_preview(self, entry: dict[str, Any], report: dict[str, Any]) -> None:
        entry["预览"] = {
            "generated_at": _now(),
            "预演页数": report["预演页数"],
            "待领页": report["待领页"],
            "重叠": report["overlaps"],
            "页状态": {detail["页面"]: detail["状态"] for detail in report["pages"]},
        }
        persist.append_jsonl("reports", "collection_preview.jsonl", {
            "trigger": "manifest_preview",
            "清单编号": report["清单编号"],
            "关联计划": report["关联计划"],
            "种子页范围": report["种子页范围"],
            "预演页数": report["预演页数"],
            "待领页": report["pending_pages"],
            "重叠": report["overlaps"],
            "页状态": entry["预览"]["页状态"],
            "timestamp": _now(),
        })

    def preview(self, entry_id: int | None = None) -> dict[str, Any]:
        """预演视图：单个清单或全部清单的重叠关系与页面归属。"""
        if entry_id is not None:
            entry = self.get_entry(entry_id)
            if entry is None:
                raise KeyError(entry_id)
            return self.build_preview(entry)
        return self.preview_all()

    def preview_all(self) -> dict[str, Any]:
        manifests = list(store.rows(MANIFEST_MODULE))
        items = [self.build_preview(entry) for entry in manifests]

        overlaps: list[dict[str, Any]] = []
        for index, left in enumerate(manifests):
            left_pages = _manifest_pages(left)
            for right in manifests[index + 1:]:
                right_pages = set(_manifest_pages(right))
                shared = [page for page in left_pages if page in right_pages]
                if shared:
                    overlaps.append({
                        "清单A": _manifest_code(left),
                        "清单B": _manifest_code(right),
                        "关联计划A": _text(left.get("关联计划")),
                        "关联计划B": _text(right.get("关联计划")),
                        "重叠页": shared,
                        "重叠数": len(shared),
                    })

        covered_pages: list[str] = []
        for manifest in manifests:
            covered_pages.extend(_manifest_pages(manifest))
        covered_pages = _dedupe(covered_pages)
        overlap_pages = sorted({
            page for page in covered_pages
            if sum(1 for manifest in manifests if page in _manifest_pages(manifest)) >= 2
        }, key=covered_pages.index)

        return {
            "generated_at": _now(),
            "summary": {
                "清单数": len(manifests),
                "预演页数": sum(len(_manifest_pages(entry)) for entry in manifests),
                "覆盖页数": len(covered_pages),
                "重叠页数": len(overlap_pages),
                "重叠页": overlap_pages,
                "已处理页数": sum(1 for page in covered_pages if page_status(page) == "已处理"),
                "已派发页数": sum(1 for page in covered_pages if page_status(page) == "已派发"),
                "可领页数": sum(1 for page in covered_pages if page_status(page) == "可领"),
            },
            "manifests": items,
            "overlaps": overlaps,
            "page_trace": [self.page_trace_for(page) for page in covered_pages],
            "batches": self.batch_history(),
        }

    # -- 派发 ---------------------------------------------------------------

    def dispatch_batch(self, entry_id: int) -> tuple[bool, str]:
        entry = self.get_entry(entry_id)
        if entry is None:
            return False, f"{self.ENTITY} {entry_id} 不存在或已归档"
        pages = _manifest_pages(entry)
        if not pages:
            return False, f"清单{_manifest_code(entry)}没有种子页，无法派发批次"

        claimable = [page for page in pages if page_status(page) == "可领"]
        skipped = [page for page in pages if page not in claimable]
        if not claimable:
            return False, (
                f"清单{_manifest_code(entry)}没有可派发页面："
                f"{'、'.join(skipped)}已由其他批次领取或已处理"
            )

        batch_rows = store.rows(BATCH_MODULE)
        new_id = max((_as_int(row.get("id")) for row in batch_rows), default=0) + 1
        preview_count = len(pages)
        batch = {
            "id": new_id,
            "批次编号": f"BATC-MANI-{new_id:04d}",
            "关联计划": _text(entry.get("关联计划")),
            "来源清单": _manifest_code(entry),
            "来源清单ID": entry.get("id"),
            "关联脚本": _text(entry.get("关联脚本")),
            "任务数量": len(claimable),
            "预演页数": preview_count,
            "实际页数": len(claimable),
            "页面范围": ",".join(claimable),
            "完成数": "0",
            "失败数": "0",
            "批次状态": "由清单派发",
            "截止时间": _text(entry.get("截止时间")) or _now(),
            "优先级": _text(entry.get("优先级")) or "中",
            "派发时间": _now(),
            "status": "排队中",
            "pending": True,
            "abnormal": False,
            "version": "",
            "updated_at": _now(),
        }
        batch["version"] = _version_id(batch)
        batch_rows.append(batch)
        for page in claimable:
            _claim_page(page, batch)

        _refresh_coverage()
        for manifest in store.rows(MANIFEST_MODULE):
            _refresh_counts(manifest)
        _save_ledger()

        persist.append_jsonl("runs", "batch_queue.jsonl", {
            "module": BATCH_MODULE, "entity": "采集批次", "entry_id": new_id,
            "status": "排队中", "version": batch["version"], "action": "auto_create_from_manifest",
            "timestamp": _now(), "abnormal": False,
            "source_module": MANIFEST_MODULE, "source_id": entry.get("id"),
            "来源清单": _manifest_code(entry), "页面范围": batch["页面范围"],
        })

        message = (
            f"已按清单{_manifest_code(entry)}派发批次{batch['批次编号']}："
            f"预演{preview_count}页，实际派出{len(claimable)}页（{'、'.join(claimable)}）"
        )
        if skipped:
            message += f"；跳过{len(skipped)}页（{'、'.join(skipped)}）已由其他批次领取或已处理"
        return True, message

    # -- 结算 ---------------------------------------------------------------

    def settle_manifest(self, entry_id: int) -> tuple[bool, str]:
        entry = self.get_entry(entry_id)
        if entry is None:
            return False, f"{self.ENTITY} {entry_id} 不存在或已归档"
        pages = _manifest_pages(entry)

        # 以已完成的批次为准补齐结算，保证重叠页也能带入相关清单。
        for batch in store.rows(BATCH_MODULE):
            if _text(batch.get("status")) != "已完成":
                continue
            shared = [page for page in pages if page in set(_batch_pages(batch))]
            if not shared:
                continue
            run = _run_for_batch(_text(batch.get("批次编号")))
            for page in shared:
                _record_settlement(entry, page, batch, run, _text(batch.get("来源清单")))

        details = list(entry.get("结算明细", []))
        if not details:
            return False, f"清单{_manifest_code(entry)}暂无可结算页面：关联批次尚未确认完成"

        settled_pages = _dedupe([_text(detail.get("页面")) for detail in details])
        entry["结算页数"] = len(settled_pages)
        entry["结算时间"] = _now()
        _refresh_counts(entry)
        _save_ledger()
        return True, (
            f"清单{_manifest_code(entry)}已结算{len(settled_pages)}页："
            f"{'、'.join(settled_pages)}"
        )

    # -- 追溯 ---------------------------------------------------------------

    def _batch_view(self, batch: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": batch.get("id"),
            "批次编号": _text(batch.get("批次编号")),
            "关联计划": _text(batch.get("关联计划")),
            "来源清单": _text(batch.get("来源清单")),
            "预演页数": _as_int(batch.get("预演页数"), _as_int(batch.get("任务数量"))),
            "实际页数": _as_int(batch.get("实际页数"), _as_int(batch.get("任务数量"))),
            "页面范围": _text(batch.get("页面范围")),
            "页面列表": _batch_pages(batch),
            "任务数量": _as_int(batch.get("任务数量")),
            "批次状态": _text(batch.get("批次状态")),
            "status": _text(batch.get("status")),
            "派发时间": _text(batch.get("派发时间")),
            "截止时间": _text(batch.get("截止时间")),
        }

    def batch_history(self) -> list[dict[str, Any]]:
        rows = sorted(store.rows(BATCH_MODULE), key=lambda row: _as_int(row.get("id")), reverse=True)
        return [
            self._batch_view(batch) for batch in rows
            if _text(batch.get("来源清单")) or _text(batch.get("页面范围"))
        ]

    def page_trace_for(self, page: str) -> dict[str, Any]:
        row = _ledger_row(page, create=True)
        assert row is not None
        records: list[dict[str, Any]] = []
        for record in row.get("批次记录", []):
            batch = _batch_by_record(record)
            status = _text(batch.get("status")) if batch else _text(record.get("批次状态"))
            view = self._batch_view(batch) if batch else {}
            records.append({
                "批次编号": _text(record.get("批次编号")),
                "来源清单": _text((batch or {}).get("来源清单")) or _text(record.get("清单编号")),
                "预演页数": _as_int((batch or {}).get("预演页数"), _as_int((batch or {}).get("任务数量"))),
                "实际页数": _as_int((batch or {}).get("实际页数"), _as_int((batch or {}).get("任务数量"))),
                "页面范围": _text((batch or {}).get("页面范围")),
                "派发时间": _text(record.get("派发时间")),
                "状态": status,
                "页面": page,
                "批次": view,
            })

        completed = [record for record in records if record["状态"] == "已完成"]
        active = [record for record in records if record["状态"] in _ACTIVE_STATUS]
        chosen = completed[-1] if completed else (active[-1] if active else (records[-1] if records else None))

        run = _run_for_batch(chosen["批次编号"]) if chosen else None
        status = page_status(page)
        return {
            "页面": page,
            "状态": status,
            "采集批次": chosen["批次编号"] if chosen and status == "已处理" else "",
            "当前批次": chosen["批次编号"] if chosen and status == "已派发" else "",
            "来源清单": chosen["来源清单"] if chosen else "",
            "派发时间": chosen["派发时间"] if chosen else "",
            "结算时间": _text(row.get("结算时间")) if status == "已处理" else "",
            "运行记录": _text((run or {}).get("记录编号")),
            "运行记录ID": (run or {}).get("id"),
            "覆盖清单": list(row.get("覆盖清单", [])),
            "批次历史": records,
        }

    def page_trace(self) -> list[dict[str, Any]]:
        return [self.page_trace_for(row.get("页面", "")) for row in list(_ledger_rows())]

    # -- 统计 ---------------------------------------------------------------

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        manifests = store.rows(self.MODULE)
        pages: list[str] = []
        for manifest in manifests:
            pages.extend(_manifest_pages(manifest))
        pages = _dedupe(pages)
        base["manifest_pages"] = len(pages)
        base["overlap_pages"] = sum(
            1 for page in pages
            if sum(1 for manifest in manifests if page in _manifest_pages(manifest)) >= 2
        )
        base["settled_pages"] = sum(1 for page in pages if page_status(page) == "已处理")
        base["dispatched_batches"] = sum(
            1 for batch in store.rows(BATCH_MODULE) if _text(batch.get("来源清单"))
        )
        return base
