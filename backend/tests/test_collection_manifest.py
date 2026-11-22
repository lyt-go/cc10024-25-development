"""定时采集执行清单集成测试：预演、重叠派发、页面追溯与结算带入。"""
from __future__ import annotations

import os
import sys

backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app import persistence as persist

for sub in ["runs", "snapshots", "reports", "exports"]:
    p = persist.data_path(sub, "")
    if os.path.isdir(p):
        for name in os.listdir(p):
            os.remove(os.path.join(p, name))


def _manifest():
    from app.services import service_for
    return service_for("collection_manifest")


def _batch():
    from app.services import service_for
    return service_for("batch_queue")


def _make_manifests(code_a: str, code_b: str, pages_a: str, pages_b: str):
    """两份共享一个中间页的清单，例如 甲乙 / 乙丙。"""
    svc = _manifest()
    first, missing = svc.create_from_script({
        "清单编号": code_a, "计划编号": "SCRI-0003", "页面": pages_a,
    })
    assert first is not None, missing
    second, missing = svc.create_entry({
        "清单编号": code_b, "关联计划": "SCRI-0003", "种子页范围": pages_b,
    })
    assert second is not None, missing
    return first, second


def _batch_by_manifest(code: str) -> dict:
    return [b for b in _batch().list_entries(size=100)[0] if b.get("来源清单") == code][0]


def test_parse_pages_from_script_input():
    svc = _manifest()
    from app.services.collection_manifest import parse_pages

    assert parse_pages("甲,乙") == ["甲", "乙"]
    assert parse_pages("甲、乙；丙") == ["甲", "乙", "丙"]
    assert parse_pages("甲乙") == ["甲", "乙"]
    assert parse_pages(["甲", "乙,甲"]) == ["甲", "乙"]

    entry, missing = svc.create_from_script({"计划编号": "SCRI-0003", "页面": "丁,戊,丁"})
    assert entry is not None, missing
    assert entry["预演页数"] == 2
    assert entry["待领页"] == 2


def test_preview_lists_overlap_between_manifests():
    svc = _manifest()
    first, _ = _make_manifests("TEST-MANI-A", "TEST-MANI-B", "申,酉", "酉,戌")

    report = svc.build_preview(first)
    assert report["预演页数"] == 2
    assert report["pending_pages"] == ["申", "酉"]
    assert any(
        overlap["清单编号"] == "TEST-MANI-B" and overlap["重叠页"] == ["酉"]
        for overlap in report["overlaps"]
    ), report["overlaps"]

    whole = svc.preview_all()
    pair = [item for item in whole["overlaps"]
            if {item["清单A"], item["清单B"]} == {"TEST-MANI-A", "TEST-MANI-B"}]
    assert pair and pair[0]["重叠页"] == ["酉"]
    assert "酉" in whole["summary"]["重叠页"]


def test_dispatch_skips_pages_already_claimed():
    """首批执行期间发起下一批：乙已在首批里，第二份清单只能领到丙。"""
    svc = _manifest()
    batches = _batch()
    first, second = _make_manifests("TEST-MANI-C", "TEST-MANI-D", "甲,乙", "乙,丙")

    ok, message = svc.run_action(first["id"], "生成预演")
    assert ok is not None, message
    ok, message = svc.dispatch_batch(first["id"])
    assert ok, message
    first_batch = _batch_by_manifest("TEST-MANI-C")
    assert first_batch["页面范围"] == "甲,乙"
    assert first_batch["预演页数"] == 2 and first_batch["实际页数"] == 2

    # 首批仍在执行，尚未结算
    batches.run_action(first_batch["id"], "发起执行")
    from app.store import store
    assert store.find("batch_queue", first_batch["id"])["status"] == "执行中"

    ok, message = svc.dispatch_batch(second["id"])
    assert ok, message
    second_batch = _batch_by_manifest("TEST-MANI-D")
    assert second_batch["页面范围"] == "丙", second_batch["页面范围"]
    assert second_batch["预演页数"] == 2 and second_batch["实际页数"] == 1
    assert "乙" in message and "跳过" in message

    trace = {item["页面"]: item for item in svc.page_trace()}
    assert trace["乙"]["当前批次"] == first_batch["批次编号"]
    assert trace["丙"]["当前批次"] == second_batch["批次编号"]
    assert trace["乙"]["状态"] == "已派发" and trace["丙"]["状态"] == "已派发"


def test_settlement_brings_overlap_pages_back():
    """首批完成后，重叠页乙沉淀到两份清单的结算里，来源指向真正的采集批次。"""
    svc = _manifest()
    batches = _batch()
    first, second = _make_manifests("TEST-MANI-E", "TEST-MANI-F", "子,丑", "丑,寅")
    svc.dispatch_batch(first["id"])
    svc.dispatch_batch(second["id"])

    first_batch = _batch_by_manifest("TEST-MANI-E")
    batches.run_action(first_batch["id"], "确认完成")

    from app.store import store
    run_rows = [r for r in store.rows("run_history")
                if r.get("来源批次") == first_batch["批次编号"]]
    assert run_rows, "批次完成应写入带来源批次的运行记录"
    assert run_rows[0]["页面范围"] == "子,丑"
    assert run_rows[0]["来源清单"] == "TEST-MANI-E"

    trace = {item["页面"]: item for item in svc.page_trace()}
    assert trace["子"]["状态"] == "已处理"
    assert trace["子"]["采集批次"] == first_batch["批次编号"]
    assert trace["丑"]["采集批次"] == first_batch["批次编号"]
    assert trace["丑"]["运行记录"] == run_rows[0]["记录编号"]
    assert trace["寅"]["状态"] == "已派发"

    first_details = {d["页面"]: d for d in svc.get_entry(first["id"])["结算明细"]}
    second_details = {d["页面"]: d for d in svc.get_entry(second["id"])["结算明细"]}
    assert set(first_details) == {"子", "丑"}
    assert first_details["丑"]["页面来源"] == "本清单派发"
    assert set(second_details) == {"丑"}
    assert second_details["丑"]["页面来源"] == "重叠带入"
    assert second_details["丑"]["来源清单"] == "TEST-MANI-E"
    assert second_details["丑"]["来源批次"] == first_batch["批次编号"]

    # 第二份清单里丑已经处理、寅仍在途，下一批没有可再领的页面
    preview = svc.build_preview(second)
    assert preview["settled_pages"] == ["丑"]
    assert preview["claimed_pages"] == ["寅"]
    assert preview["pending_pages"] == []

    # 确认结算会把已处理的页面固定进清单结算
    result, message = svc.run_action(second["id"], "确认结算")
    assert result is not None, message
    assert result["status"] == "已结算"


def test_manifest_domain_snapshot_contract():
    svc = _manifest()
    snap = svc.domain_snapshot()
    for key in ("module", "entity", "artifact", "count", "by_status", "abnormal"):
        assert key in snap
    assert snap["manifest_pages"] >= 1
    assert snap["overlap_pages"] >= 1
    assert snap["dispatched_batches"] >= 1
    assert snap["settled_pages"] >= 1


if __name__ == "__main__":
    test_parse_pages_from_script_input()
    print("PASS: test_parse_pages_from_script_input")
    test_preview_lists_overlap_between_manifests()
    print("PASS: test_preview_lists_overlap_between_manifests")
    test_dispatch_skips_pages_already_claimed()
    print("PASS: test_dispatch_skips_pages_already_claimed")
    test_settlement_brings_overlap_pages_back()
    print("PASS: test_settlement_brings_overlap_pages_back")
    test_manifest_domain_snapshot_contract()
    print("PASS: test_manifest_domain_snapshot_contract")
    print("\nALL 5 MANIFEST TESTS PASSED")
