"""营业日签发的应用服务：解析数据来源 + 合并持久化入账 + 幂等签发。"""

from __future__ import annotations

import json
import os
from typing import Any

from app.errors import DomainIssue
from app.reconcile.daily import (
    CrossDayLocked,
    build_daily_board,
    plan_entries,
)

DEFAULT_SCOPE = "default"


def _pipeline_source(profile: str = "clean", path: str | None = None):
    # 延迟导入，避免 app.service <-> reconcile 的循环依赖
    from app.service import pipeline

    return pipeline(profile or "clean", path)


def source_from_run(row: dict) -> Any:
    payload = json.loads(row["payload"])
    if isinstance(payload.get("daily"), dict):
        return payload["daily"]
    if isinstance(payload.get("settlement"), dict):
        return payload["settlement"]
    # 旧批次没有 daily 快照时，用其 profile 重算
    return _pipeline_source(payload.get("profile", "clean"))


def resolve_source(
    store,
    *,
    run_id: str | None = None,
    profile: str | None = None,
    path: str | None = None,
    body: dict | None = None,
):
    """按显式 rows > run_id > path/profile > 最近批次 > clean 样例解析。"""
    body = body or {}

    explicit = body.get("results") or body.get("rows") or body.get("trips") or body.get("items")
    if isinstance(explicit, list) and explicit:
        return explicit, {"run_id": run_id, "profile": profile or body.get("profile"), "source": "body"}

    run_id = run_id or body.get("run_id") or body.get("id")
    if run_id:
        row = store.get(run_id)
        if row is None:
            raise DomainIssue("RUN_NOT_FOUND", 404)
        return source_from_run(row), {"run_id": run_id, "profile": row.get("profile"), "source": "run"}

    path = path or body.get("path") or body.get("file_path")
    profile = profile or body.get("profile")
    if path:
        return _pipeline_source(profile or "clean", path), {"profile": profile or "clean", "path": path, "source": "path"}

    if profile:
        return _pipeline_source(profile), {"profile": profile, "source": "profile"}

    latest = store.latest() if store is not None else None
    if latest is not None:
        return source_from_run(latest), {"run_id": latest["id"], "profile": latest.get("profile"), "source": "latest"}

    return _pipeline_source("clean"), {"profile": "clean", "source": "default"}


def board_for(store, *, scope: str = DEFAULT_SCOPE, **resolve_kwargs) -> dict:
    source, meta = resolve_source(store, **resolve_kwargs)
    posted = store.posted_fares(scope) if store is not None else {}
    board = build_daily_board(source, posted)
    board["meta"] = meta
    return board


def sign_days(
    store,
    body: dict,
    *,
    scope: str = DEFAULT_SCOPE,
    run_id: str | None = None,
) -> dict:
    source, meta = resolve_source(store, run_id=run_id, body=body)
    posted = store.posted_fares(scope)

    target_date = (
        body.get("business_date")
        or body.get("date")
        or body.get("day")
        or body.get("biz_date")
    )
    dates = body.get("dates") or body.get("business_dates") or body.get("days")
    if isinstance(dates, str):
        dates = [part.strip() for part in dates.split(",") if part.strip()]
    if target_date and not dates:
        dates = [target_date]

    trip_id = body.get("trip_id") or body.get("trip") or body.get("key")
    all_eligible = bool(
        body.get("all") or body.get("all_eligible") or body.get("eligible") or body.get("sign_all")
    )

    try:
        entries = plan_entries(
            source,
            posted,
            dates=dates,
            trip_id=trip_id,
            all_eligible=all_eligible,
            business_date=None if dates else target_date,
        )
    except CrossDayLocked as exc:
        raise DomainIssue(
            "CROSS_DAY_LOCKED",
            409,
            f"跨日骑行 {exc.trip_id} 涉及的营业日仍有未核清扣款: {','.join(exc.blocking_dates)}",
        )

    result = store.post_fares(entries, scope=scope, run_id=meta.get("run_id"))
    board = _reboard(store, source, meta)

    target_day = None
    if target_date:
        from app.reconcile.daily import iso_date

        wanted = iso_date(target_date)
        target_day = next((d for d in board["days"] if str(d["business_date"]) == wanted), None)

    return {
        "ok": True,
        "board": board,
        "settlement": board,
        "daily": board,
        "days": board["days"],
        "business_days": board["business_days"],
        "totals": board["totals"],
        "posted": result["entries"],
        "entries": result["entries"],
        "inserted": result["inserted"],
        "created": result["inserted"],
        "duplicated": result["duplicated"],
        "idempotent": True,
        "meta": meta,
        "day": target_day,
        "business_day": target_day,
    }


def _reboard(store, source, meta):
    """签发后用同一数据来源 + 最新入账记录重建看板。"""
    posted = store.posted_fares(DEFAULT_SCOPE)
    board = build_daily_board(source, posted)
    board["meta"] = meta
    return board


def enrich_run(run: dict, store) -> dict:
    if not isinstance(run, dict):
        return run
    source = run.get("daily") or run.get("settlement")
    if not source:
        return run
    try:
        posted = store.posted_fares(DEFAULT_SCOPE) if store is not None else {}
        board = build_daily_board(source, posted)
        board["meta"] = {"run_id": run.get("run_id"), "source": "run"}
        run["daily"] = board
        run["daily_settlement"] = board
        run["settlement"] = board
        run["days"] = board["business_days"]
        run["business_days"] = board["business_days"]
    except Exception:  # pragma: no cover - 富化失败不应影响主响应
        pass
    return run


class DailySettlementService:
    """面向对象风格的薄封装，兼容 ``DailySettlementService(store).sign(...)``。"""

    def __init__(self, store=None, scope: str = DEFAULT_SCOPE):
        # 兼容直接传 SQLite 路径的构造方式
        if store is not None and not hasattr(store, "posted_fares"):
            from pathlib import Path

            if isinstance(store, (str, bytes, os.PathLike)):
                from app.review.store import Store

                store = Store(str(Path(store)))
        self.store = store
        self.scope = scope

    def board(self, **kwargs):
        return board_for(self.store, scope=self.scope, **kwargs)

    get_board = board
    list_days = board
    daily_board = board

    def sign(self, body: dict | None = None, **kwargs):
        data = dict(body or {})
        data.update(kwargs)
        run_id = data.pop("run_id", None)
        return sign_days(self.store, data, scope=self.scope, run_id=run_id)

    sign_day = sign
    sign_business_day = sign
    post = sign
    approve = sign

    def posted_fares(self):
        return self.store.posted_fares(self.scope) if self.store is not None else {}


SettlementService = DailySettlementService
DailyService = DailySettlementService

