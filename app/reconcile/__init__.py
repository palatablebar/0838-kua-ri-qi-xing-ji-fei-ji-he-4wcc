"""对账包：批次台账 + 按营业日签发。"""

from app.reconcile.daily import (  # noqa: F401
    build_daily_board,
    build_daily_ledger,
    build_daily_settlement,
    daily_board,
    daily_ledger,
    plan_entries,
    sign_source,
)


def __getattr__(name):
    # 兼容 ``from app.reconcile import sign_days`` 等写法
    if name in {"sign_days", "board_for"}:
        from app.reconcile.settlement_service import board_for, sign_days

        return {"sign_days": sign_days, "board_for": board_for}[name]
    from app.reconcile import daily

    return getattr(daily, name)
