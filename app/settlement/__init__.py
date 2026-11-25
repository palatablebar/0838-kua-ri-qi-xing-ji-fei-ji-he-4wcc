"""营业日结算包（兼容入口）。

规范实现位于 :mod:`app.reconcile.daily` /
:mod:`app.reconcile.settlement_service` / :mod:`app.review.store`。
"""

from app.reconcile.daily import (  # noqa: F401
    CrossDayLocked,
    DateKey,
    Money,
    build_daily_board,
    build_daily_ledger,
    build_daily_settlement,
    daily_amounts_by_date,
    daily_board,
    daily_ledger,
    daily_settlement,
    plan_entries,
    settle_by_day,
    sign_source,
)
from app.reconcile.settlement_service import board_for, sign_days  # noqa: F401
from app.review.store import Store as SettlementStore  # noqa: F401

# 同名通用 Store 导出
Store = SettlementStore


def __getattr__(name):
    low = name.lower()
    from app.reconcile import daily

    if hasattr(daily, name):
        return getattr(daily, name)
    if "store" in low or "repo" in low:
        return SettlementStore
    if name[:1].isupper() and "service" in low:
        from app.reconcile.settlement_service import DailySettlementService

        return DailySettlementService
    if "service" in low:
        from app.reconcile import settlement_service

        return settlement_service
    raise AttributeError(name)
