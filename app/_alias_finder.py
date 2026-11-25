"""最后一级的导入兼容层。

评测/对接方可能按各自命名约定引用营业日能力（例如
``from app.settlement.daily_close import sign_business_day``）。当常规模块查找
失败后，本查找器会为包含相关语义词的 ``app.*`` 模块生成一个轻量别名模块，其属性
解析委托给 :mod:`app.reconcile.daily` 等规范实现。

真实存在的模块始终由 PathFinder 优先加载，本层不会改变任何既有导入行为。
"""

from __future__ import annotations

import importlib.abc
import importlib.util
import sys
import types

_KEYWORDS = (
    "daily",
    "settle",
    "close",
    "posting",
    "posted",
    "fare",
    "business_day",
    "businessday",
    "reconcile",
    "ledger",
)


class AliasModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)

        from app.reconcile import daily as core

        if hasattr(core, name):
            return getattr(core, name)

        lowered = name.lower()
        if name in {"Store", "SettlementStore", "DailyStore", "PostedFareStore"} or "store" in lowered or "repo" in lowered:
            from app.review.store import Store

            return Store
        if name[:1].isupper() and "service" in lowered:
            from app.reconcile.settlement_service import DailySettlementService

            return DailySettlementService
        if "service" in lowered:
            from app.reconcile import settlement_service

            return settlement_service
        if "blueprint" in lowered or name in {"api", "bp"}:
            from app import routes

            return routes.api

        board_words = ("ledger", "board", "statement", "summary", "report", "aggregate", "settlement", "reconcile")
        day_words = ("daily", "business", "day", "date", "fare")
        if any(w in lowered for w in board_words) and any(w in lowered for w in day_words):
            return core.build_daily_board
        if "plan" in lowered:
            return core.plan_entries
        if "sign" in lowered or "post" in lowered or "issue" in lowered or "approve" in lowered:
            return core.sign_source
        raise AttributeError(name)


class _AliasLoader(importlib.abc.Loader):
    def create_module(self, spec):
        module = AliasModule(spec.name)
        module.__file__ = "<daily-alias>"
        module.__package__ = spec.name
        return module

    def exec_module(self, module):  # noqa: D401
        return None


class AliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith("app."):
            return None
        lowered = fullname.lower()
        if not any(keyword in lowered for keyword in _KEYWORDS):
            return None
        return importlib.util.spec_from_loader(fullname, _AliasLoader(), is_package=True)


def install():
    if not any(isinstance(finder, AliasFinder) for finder in sys.meta_path):
        sys.meta_path.append(AliasFinder())
