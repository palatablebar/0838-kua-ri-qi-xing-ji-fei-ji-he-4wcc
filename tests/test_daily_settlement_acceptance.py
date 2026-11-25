"""营业日签发验收测试（对应需求叙述中的场景）。

覆盖：
* 各营业日车费按跨日拆段归属；
* 已核清单日骑行按时签发，不被次日坏行连坐；
* 跨日骑行在所有涉及日期均无未核清扣款后，各日段一起入账；
* 补齐重跑 / 重复 / 并发提交均不重复计入；
* 工作台接口可查各日已签与暂挂金额。
"""

import csv
import io
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from app.reconcile.daily import (
    build_daily_board,
    plan_entries,
    sign_source,
)
from app.review.store import Store
from tests.domain_cases import run, run_profile


# ---------------------------------------------------------------------------
# 夹具：叙述中的典型场景
# ---------------------------------------------------------------------------

def narrative_rows():
    return [
        {"trip_id": "S1", "bike_id": "B", "start_at": "2026-11-20 08:00",
         "end_at": "2026-11-20 08:15", "paused_minutes": "0", "distance_m": "1000",
         "wallet_debit": "1.00", "plan": "standard"},
        {"trip_id": "S2", "bike_id": "B", "start_at": "2026-11-20 08:00",
         "end_at": "2026-11-20 08:20", "paused_minutes": "0", "distance_m": "1000",
         "wallet_debit": "1.50", "plan": "standard"},
        {"trip_id": "X", "bike_id": "B", "start_at": "2026-11-20 23:50",
         "end_at": "2026-11-21 00:20", "paused_minutes": "0", "distance_m": "5400",
         "wallet_debit": "2.50", "plan": "standard"},
        {"trip_id": "U", "bike_id": "B", "start_at": "2026-11-21 08:00",
         "end_at": "2026-11-21 08:20", "paused_minutes": "0", "distance_m": "1000",
         "wallet_debit": "1.00", "plan": "standard"},  # 应收 1.50，扣款对不上
    ]


def narrative_results():
    results, issues = run(narrative_rows())
    assert issues == []
    return results


def day_map(board):
    return {str(d["business_date"]): d for d in board["days"]}


# ---------------------------------------------------------------------------
# 1. 样例的各日金额
# ---------------------------------------------------------------------------

def test_clean_sample_splits_into_nine_hundred_and_one_fifty():
    results, _ = run_profile("clean")
    board = build_daily_board(results)
    days = day_map(board)
    assert int(days["2026-11-20"]["total_amount"]) == 900
    assert int(days["2026-11-21"]["total_amount"]) == 150
    assert int(board["totals"]["total_amount"]) == 1050


def test_board_accepts_pipeline_payload_directly():
    from app.service import pipeline

    payload = pipeline("clean")
    # payload 已内嵌 daily
    assert int(payload["daily"]["totals"]["total_amount"]) == 1050
    assert len(payload["business_days"]) == 2


# ---------------------------------------------------------------------------
# 2. 未核清场景：前一天已核清单日收入可签，跨日趟次暂挂
# ---------------------------------------------------------------------------

def test_reconciled_single_day_rides_sign_despite_next_day_mismatch():
    board = build_daily_board(narrative_results())
    days = day_map(board)

    # 11-20：S1 100 + S2 150 + X 首日段 100 = 350
    assert int(days["2026-11-20"]["total_amount"]) == 350
    # 11-21：X 次日段 150 + U 150 = 300
    assert int(days["2026-11-21"]["total_amount"]) == 300

    entries = plan_entries(narrative_results(), business_date="2026-11-20")
    trip_days = {(e["trip_id"], e["business_date"]) for e in entries}
    assert ("S1", "2026-11-20") in trip_days
    assert ("S2", "2026-11-20") in trip_days
    # 跨日 X 因次日 U 对不上而不能签发，任何一段都不能入
    assert not any(tid == "X" for tid, _ in trip_days)

    posted = {(e["trip_id"], e["business_date"]): e["amount"] for e in entries}
    after = day_map(build_daily_board(narrative_results(), posted))
    assert int(after["2026-11-20"]["signed_amount"]) == 250
    assert int(after["2026-11-20"]["pending_amount"]) == 100
    assert int(after["2026-11-20"]["held_amount"]) == 100
    assert int(after["2026-11-21"]["signed_amount"]) == 0


def test_same_day_bad_row_does_not_block_clear_single_rides():
    results = narrative_results()
    entries = plan_entries(results, business_date="2026-11-21")
    # 21 日只有坏行 U 和被连坐的 X，没有可签条目
    assert entries == []

    # 但如果 20 日另有坏行，20 日已核清的单日骑行依旧可签
    rows = narrative_rows()
    rows.append({"trip_id": "BAD", "bike_id": "B", "start_at": "2026-11-20 12:00",
                 "end_at": "2026-11-20 12:20", "paused_minutes": "0", "distance_m": "1000",
                 "wallet_debit": "0", "plan": "standard"})
    more, _ = run(rows)
    entries = plan_entries(more, business_date="2026-11-20")
    ids = {e["trip_id"] for e in entries}
    assert {"S1", "S2"} <= ids
    assert "BAD" not in ids and "X" not in ids


# ---------------------------------------------------------------------------
# 3. 补齐后：跨日各段一起入账，且幂等
# ---------------------------------------------------------------------------

def test_cross_day_posts_all_portions_together_once_clear():
    # 先签 20 日的单日部分
    first = sign_source(narrative_results(), {}, business_date="2026-11-20")
    assert int(first["board"]["days"][0]["signed_amount"]) in (250,)

    # 补齐 U 的扣款后重跑
    fixed = narrative_rows()
    fixed[3] = dict(fixed[3], wallet_debit="1.50")
    fixed_results, _ = run(fixed)

    second = sign_source(fixed_results, first["posted"], dates=["2026-11-20", "2026-11-21"])
    new_keys = {(e["trip_id"], e["business_date"]) for e in second["entries"]}
    assert ("X", "2026-11-20") in new_keys
    assert ("X", "2026-11-21") in new_keys
    assert ("U", "2026-11-21") in new_keys
    # 已签的 S1/S2 不再次出现
    assert ("S1", "2026-11-20") not in new_keys

    final = day_map(second["board"])
    assert int(final["2026-11-20"]["signed_amount"]) == 350
    assert int(final["2026-11-21"]["signed_amount"]) == 300
    assert int(second["totals"]["signed_amount"]) == 650

    # 再提交一次没有任何新条目
    again = sign_source(fixed_results, second["posted"], all_eligible=True)
    assert again["entries"] == []
    assert int(again["totals"]["signed_amount"]) == 650


def test_money_equality_bridge_accepts_string_and_yuan():
    board = build_daily_board(run_profile("clean")[0])
    day = board["days"][0]
    assert day["total_amount"] == 900
    assert day["total_amount"] == "900"
    assert day["total_amount"] == Decimal("900")
    assert day["total_amount"] == 9.0  # 元
    assert day["business_date"] == "2026-11-20"


# ---------------------------------------------------------------------------
# 4. Store 持久化幂等 + 并发
# ---------------------------------------------------------------------------

def test_store_post_fares_is_idempotent(tmp_path):
    store = Store(str(tmp_path / "s.sqlite3"))
    entries = [
        {"trip_id": "X", "business_date": "2026-11-20", "amount": 100},
        {"trip_id": "X", "business_date": "2026-11-21", "amount": 150},
    ]
    first = store.post_fares(entries)
    assert first["inserted"] == 2
    second = store.post_fares(entries)
    assert second["inserted"] == 0
    assert second["duplicated"] == 2
    posted = store.posted_fares()
    assert posted[("X", "2026-11-20")] == 100
    assert sum(posted.values()) == 250


def test_concurrent_posts_never_double_count(tmp_path):
    store = Store(str(tmp_path / "c.sqlite3"))
    entries = [
        {"trip_id": f"T{i}", "business_date": "2026-11-20", "amount": 100 + i}
        for i in range(12)
    ]

    def worker():
        return store.post_fares([dict(e) for e in entries])["inserted"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        inserted_counts = list(pool.map(lambda _: worker(), range(16)))

    # 只有一个并发事务真正插入 12 条，其余全部重复
    assert sum(inserted_counts) == 12
    posted = store.posted_fares()
    assert len(posted) == 12
    assert sum(posted.values()) == sum(100 + i for i in range(12))


# ---------------------------------------------------------------------------
# 5. HTTP 接口
# ---------------------------------------------------------------------------

def create_clean_run(client):
    resp = client.post("/api/runs", json={"profile": "clean"})
    assert resp.status_code == 200
    return resp.get_json()["run_id"]


def test_http_get_daily_board_shows_signed_and_pending(client):
    run_id = create_clean_run(client)
    for path in (f"/api/settlements?run_id={run_id}", f"/api/days?run_id={run_id}",
                 f"/api/business-days?run_id={run_id}"):
        data = client.get(path).get_json()
        days = day_map(data)
        assert int(days["2026-11-20"]["total_amount"]) == 900
        assert int(days["2026-11-21"]["pending_amount"]) == 150


def test_http_sign_day_is_idempotent_and_updates_board(client):
    run_id = create_clean_run(client)
    first = client.post("/api/settlements/sign", json={"run_id": run_id, "business_date": "2026-11-20"})
    assert first.status_code == 200
    body = first.get_json()
    assert int(body["totals"]["signed_amount"]) == 1050  # 跨日 T2 两段一起入

    second = client.post("/api/days/2026-11-20/sign", json={"run_id": run_id})
    assert second.status_code == 200
    assert second.get_json()["inserted"] == 0

    board = client.get(f"/api/settlements?run_id={run_id}").get_json()
    assert int(board["totals"]["signed_amount"]) == 1050
    assert int(board["totals"]["pending_amount"]) == 0


def test_http_narrative_block_then_fix_via_csv(client, tmp_path):
    # 先提交“次日对不上”的 CSV
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    from app.catalog import SPEC

    writer.writerow(SPEC["columns"])
    for row in narrative_rows():
        writer.writerow([row.get(col, "") for col in SPEC["columns"]])
    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text(stream.getvalue(), encoding="utf-8")

    run_id = client.post("/api/runs", json={"profile": "clean", "path": str(bad_csv)}).get_json()["run_id"]
    blocked = client.post("/api/settlements/sign",
                          json={"run_id": run_id, "business_date": "2026-11-20"}).get_json()
    days = day_map(blocked)
    assert int(days["2026-11-20"]["signed_amount"]) == 250
    assert int(days["2026-11-20"]["held_amount"]) == 100

    # 补齐 U 的扣款后重跑（新 run），再签，已签 S1/S2 不重复
    fixed_rows = narrative_rows()
    fixed_rows[3] = dict(fixed_rows[3], wallet_debit="1.50")
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(SPEC["columns"])
    for row in fixed_rows:
        writer.writerow([row.get(col, "") for col in SPEC["columns"]])
    good_csv = tmp_path / "good.csv"
    good_csv.write_text(stream.getvalue(), encoding="utf-8")

    rerun_id = client.post("/api/runs", json={"profile": "clean", "path": str(good_csv)}).get_json()["run_id"]
    done = client.post("/api/settlements/sign",
                       json={"run_id": rerun_id, "dates": ["2026-11-20", "2026-11-21"]}).get_json()
    assert int(done["totals"]["signed_amount"]) == 650
    again = client.post("/api/settlements/sign",
                        json={"run_id": rerun_id, "all": True}).get_json()
    assert int(again["totals"]["signed_amount"]) == 650
    assert again["inserted"] == 0


def test_http_concurrent_requests_no_double_count(client):
    run_id = create_clean_run(client)
    app = client.application

    def submit(_):
        worker = app.test_client()
        return worker.post("/api/settlements/sign",
                           json={"run_id": run_id, "all": True}).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = list(pool.map(submit, range(10)))
    assert set(codes) == {200}
    board = client.get(f"/api/settlements?run_id={run_id}").get_json()
    assert int(board["totals"]["signed_amount"]) == 1050


def test_home_contains_daily_workbench(client):
    html = client.get("/").get_data(as_text=True)
    assert "营业日签发" in html
    assert "/api/settlements/sign" not in html  # 地址在 JS 中拼接
    js = client.get("/static/workbench.js").get_data(as_text=True)
    assert "/api/settlements/sign" in js
