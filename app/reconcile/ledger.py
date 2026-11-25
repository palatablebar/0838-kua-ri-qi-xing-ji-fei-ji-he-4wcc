from decimal import Decimal

from app.catalog import SPEC
from app.reconcile.checks import all_checks
from app.reconcile.daily import (  # noqa: F401
    build_daily_board as build_daily_ledger,
)
from app.reconcile.daily import (  # noqa: F401
    build_daily_board,
    build_daily_settlement,
    daily_ledger,
)


def __getattr__(name):
    # 兼容 from app.reconcile.ledger import <其它营业日函数>
    from app.reconcile import daily

    if hasattr(daily, name):
        return getattr(daily, name)
    raise AttributeError(name)


def build_ledger(intake, prepared, prepare_issues, results):
    counts = dict(intake["counts"])
    counts["prepared"] = len(prepared)
    counts["prepare_issues"] = len(prepare_issues)
    counts["computed"] = len(results)
    balanced = counts["input"] == counts["accepted"] + counts["isolated"] + counts["duplicates"] + counts["ignored"]
    metric = sum((item.get("metric") for item in results if item.get("metric") is not None), Decimal("0"))
    checks = [
        {"id": "source", "label": "来源行守恒", "ok": balanced},
        {"id": "computed", "label": "计算条目完整", "ok": counts["computed"] == len(results)},
    ]
    daily = build_daily_board(results)
    return {"counts": counts, "balanced": balanced, "metric_field": SPEC.get("metric_field", "metric"),
            "metric_total": str(metric), "checks": checks, "all_checks": all_checks(checks),
            # 按营业日签发所需的各日金额（未合并持久化已签记录时 signed=0）
            "daily": daily,
            "daily_settlement": daily,
            "business_days": daily["business_days"],
            "days": daily["business_days"],
            "daily_totals": daily["totals"]}
