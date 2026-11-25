"""营业日结算的兼容入口（规范实现见 :mod:`app.reconcile.daily` 与
:mod:`app.reconcile.settlement_service`）。"""

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
    iso_date,
    plan_entries,
    settle_by_day,
    sign_source,
    to_cents,
    trip_portions,
    trip_is_clear,
)
from app.reconcile.settlement_service import board_for, sign_days  # noqa: F401

# 某些约定把存储类也放在 settlement 模块导出
try:  # pragma: no cover - 简单再导出
    from app.review.store import Store as SettlementStore  # noqa: F401
except Exception:  # pragma: no cover
    SettlementStore = None


def __getattr__(name):
    from app.reconcile import daily as _daily

    return getattr(_daily, name)
