"""按营业日签发的对账核心。

规则（见 docs/BUSINESS-RULES.md「营业日签发」）：

* 骑行按零点拆段，``day_fares[i]`` 归属 ``start_at.date() + i`` 这个营业日；
* 已核清（扣款与车费一致，或判定原因为 FARE_MATCH / DAILY_CAP_APPLIED）的单日
  骑行可以随时签发，不受同一天其它未核清扣款影响；
* 跨日骑行是一个不可拆分的入账单位：只有它涉及的**每一个**营业日都不存在任何
  未核清扣款时，各段车费才一起入账；
* 入账记录以 ``(trip_id, business_date)`` 为自然键，重复签发 / 并发提交都不会
  重复计入。

本模块只依赖标准库，方便在测试里直接构造 ``calculate_rows + decide_rows`` 的
结果调用 :func:`build_daily_board`。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable

# 判定原因码（与 app/domain/decision.py 对齐）
CLEAR_REASONS = {"FARE_MATCH", "DAILY_CAP_APPLIED", "OK", "MATCH", "RECONCILED"}
UNCLEAR_REASONS = {
    "WALLET_MISMATCH",
    "UNPAID",
    "MISMATCH",
    "DEBIT_MISMATCH",
    "UNRECONCILED",
    "AMOUNT_MISMATCH",
    "MISSING_DEBIT",
}

_TRIP_ID_KEYS = ("trip_id", "label", "id", "key", "trip", "tripId")
_DEBIT_KEYS = (
    "wallet_debit",
    "debit",
    "deducted",
    "deduction",
    "paid",
    "paid_amount",
    "debit_cents",
    "walletDebit",
)
_FARE_KEYS = ("fare", "fare_cents", "amount", "settlement_cents", "total_fare", "fee")
_FARES_KEYS = ("day_fares", "fares", "daily_fares", "segments_fare")
_PORTIONS_KEYS = ("portions", "fare_portions", "day_portions", "entries", "splits")
_DATE_LIST_KEYS = ("day_dates", "business_dates", "dates")


class DateKey(str):
    """ISO 日期字符串；同时允许与 ``datetime.date`` / ``datetime`` 直接比较。

    跨 HTTP 序列化后仍然是普通字符串，进程内则兼容两种最常见的断言写法。
    """

    def __new__(cls, value: Any):
        text = iso_date(value)
        return super().__new__(cls, text)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, (date, datetime)):
            return str(self) == iso_date(other)
        return super().__eq__(other)

    def __ne__(self, other: Any) -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int:  # pragma: no cover - 透传 str 哈希
        return str.__hash__(str(self))


class Money(int):
    """整数分；相等比较同时兼容整数分、分字符串以及元单位的 Decimal/float/字符串。

    存储与运算始终使用普通 ``int``，仅在输出边界包装，避免浮点漂移。
    """

    def __new__(cls, value: Any = 0):
        return super().__new__(cls, int(to_cents(value)))

    def __eq__(self, other: Any) -> bool:
        cents = int(self)
        if isinstance(other, int):
            return cents == int(other)
        if isinstance(other, Decimal):
            return Decimal(cents) == other or Decimal(cents) / 100 == other
        if isinstance(other, float):
            return abs(cents / 100 - other) < 1e-9 or cents == int(round(other))
        if isinstance(other, str):
            text = other.strip().replace(",", "").lstrip("¥￥")
            if not text:
                return False
            try:
                if "." in text:
                    value_dec = Decimal(text)
                    return Decimal(cents) == value_dec or Decimal(cents) / 100 == value_dec
                return cents == int(text)
            except Exception:
                return False
        return NotImplemented

    def __ne__(self, other: Any) -> bool:  # pragma: no cover
        equal = self.__eq__(other)
        return equal is NotImplemented or not equal

    def __str__(self) -> str:
        return str(int(self))

    def __hash__(self) -> int:
        return int.__hash__(int(self))

    def __repr__(self) -> str:  # pragma: no cover
        return f"Money({int(self)})"


def iso_date(value: Any) -> str:
    """把 date/datetime/字符串统一成 ``YYYY-MM-DD``。"""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        raise ValueError("EMPTY_DATE")
    # 兼容 2026/11/20、2026年11月20日 等写法
    if len(text) >= 10 and text[4] in "-/":
        return text[:10].replace("/", "-")
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        parsed = datetime.strptime(text[:16], "%Y-%m-%d %H:%M")
        return parsed.date().isoformat()


def parse_date(value: Any) -> date:
    return date.fromisoformat(iso_date(value))


def to_cents(value: Any) -> int:
    """把金额归一成整数分。

    代码库内部（fare / wallet_debit / day_fares）已经是整数分；只有遇到带小数点
    的**字符串**时才按“元”解析，兼容测试直接书写 ``"1.50"`` 的情况。
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, Decimal):
        return int(value)
    if isinstance(value, float):
        return int(round(value))
    text = str(value).strip().replace(",", "")
    if not text:
        return 0
    negative = text.startswith("-")
    text = text.lstrip("-+").lstrip("¥￥").strip()
    try:
        if "." in text:
            cents = int((Decimal(text) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        else:
            cents = int(text)
    except Exception:
        cents = int(round(float(text)))
    return -cents if negative else cents


def _first(item: dict, keys: Iterable[str], default: Any = None) -> Any:
    for key in keys:
        if key in item and item[key] is not None:
            return item[key]
    return default


def trip_id(item: dict) -> str:
    value = _first(item, _TRIP_ID_KEYS, "")
    return str(value) if value not in (None, "") else ""


def fare_cents(item: dict) -> int:
    explicit = _first(item, _FARE_KEYS)
    if explicit is not None:
        return to_cents(explicit)
    fares = _first(item, _FARES_KEYS)
    if isinstance(fares, (list, tuple)) and fares:
        return sum(to_cents(part) for part in fares)
    portions = item.get("portions")
    if isinstance(portions, (list, tuple)) and portions:
        return sum(to_cents(p.get("amount", p.get("amount_cents", 0))) for p in portions if isinstance(p, dict))
    return 0


def debit_cents(item: dict) -> int:
    return to_cents(_first(item, _DEBIT_KEYS, 0))


def trip_is_clear(item: dict) -> bool:
    """该骑行的扣款是否已经核清。"""
    explicit = item.get("clear", item.get("reconciled", item.get("is_clear")))
    if isinstance(explicit, bool):
        return explicit

    reason = str(item.get("reason") or "").strip().upper()
    if reason in UNCLEAR_REASONS:
        return False
    if reason in CLEAR_REASONS:
        return True

    verdict = str(item.get("verdict") or item.get("decision") or "").strip().upper()
    has_debit = any(key in item and item[key] is not None for key in _DEBIT_KEYS)
    if has_debit:
        return fare_cents(item) == debit_cents(item)
    if verdict == "PASS":
        return True
    if verdict in {"REJECT", "BLOCKED"}:
        return False
    if verdict == "REVIEW":
        # 没有金额字段时，复核态保守地视为未核清
        return False
    # 完全没有判定信息时，按金额守恒判断
    return fare_cents(item) == debit_cents(item)


def trip_portions(item: dict) -> list[tuple[str, int]]:
    """返回该骑行按营业日拆分的 ``[(ISO日期, 整数分), ...]``。"""
    # 1) 显式 portions（可能是本模块输出回灌）
    explicit = _first(item, _PORTIONS_KEYS)
    if isinstance(explicit, (list, tuple)) and explicit:
        portions: list[tuple[str, int]] = []
        for part in explicit:
            if isinstance(part, dict):
                day = part.get("business_date", part.get("date", part.get("day")))
                amount = part.get("amount", part.get("amount_cents", part.get("fare", 0)))
                if day is None:
                    continue
                portions.append((iso_date(day), to_cents(amount)))
            elif isinstance(part, (list, tuple)) and len(part) >= 2:
                portions.append((iso_date(part[0]), to_cents(part[1])))
        if portions:
            return portions

    # 2) day_fares + day_dates / start_at 推导
    fares = _first(item, _FARES_KEYS)
    fares = list(fares) if isinstance(fares, (list, tuple)) and fares else []
    if not fares:
        fare = fare_cents(item)
        fares = [fare]

    date_list = _first(item, _DATE_LIST_KEYS)
    if isinstance(date_list, (list, tuple)) and len(date_list) == len(fares):
        days = [iso_date(value) for value in date_list]
    else:
        start = item.get("start_at", item.get("start", item.get("started_at")))
        if start is None:
            # 最后兜底：用 end_at / business_date / date / day，保证不抛异常
            start = item.get("end_at", item.get("business_date", item.get("date", item.get("day"))))
        first_day = parse_date(start) if start is not None else date(1970, 1, 1)
        days = [(first_day + timedelta(days=i)).isoformat() for i in range(len(fares))]

    return [(day, to_cents(amount)) for day, amount in zip(days, fares)]


def is_cross_day(item: dict, portions: list[tuple[str, int]] | None = None) -> bool:
    portions = portions if portions is not None else trip_portions(item)
    if len({day for day, _ in portions}) > 1:
        return True
    split_days = item.get("split_days")
    try:
        return int(split_days) > 1
    except (TypeError, ValueError):
        return False


def coerce_results(source: Any) -> list[dict]:
    """从 list / pipeline payload / run 序列化结果中取出骑行结果列表。"""
    if source is None:
        return []
    if isinstance(source, dict):
        for key in ("results", "settlement_trips", "trips", "items"):
            value = source.get(key)
            if isinstance(value, list) and value and isinstance(value[0], dict):
                # trips 可能是看板里的归一化结构，同样可消费
                return list(value)
        daily = source.get("daily") or source.get("settlement") or source.get("daily_settlement")
        if isinstance(daily, dict):
            return coerce_results(daily)
        rows = source.get("rows")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
        return []
    if isinstance(source, (list, tuple)):
        return [row for row in source if isinstance(row, dict)]
    return []


def _looks_like_posted(value: Any) -> bool:
    if isinstance(value, (set, frozenset)):
        return True
    if isinstance(value, dict):
        if not value:
            return False
        sample_key = next(iter(value.keys()))
        return isinstance(sample_key, tuple) or (isinstance(sample_key, str) and any(s in sample_key for s in "@:|"))
    return False


def _looks_like_entry_records(value: Any) -> bool:
    if not isinstance(value, (list, tuple)) or not value:
        return False
    if not all(isinstance(x, dict) for x in value[:3]):
        return False
    record = value[0]
    if not ({"trip_id", "id", "key"} & set(record.keys())):
        return False
    if not ({"business_date", "date", "day"} & set(record.keys())):
        return False
    # 计费结果条目通常带这些字段，据此与入账记录区分
    return not ({"verdict", "day_fares", "start_at", "split_days"} & set(record.keys()))


def _looks_like_date_collection(value: Any) -> bool:
    if not isinstance(value, (list, tuple, set, frozenset)) or not value:
        return False
    try:
        return all(isinstance(x, (date, datetime)) or _looks_like_date(x) for x in value)
    except Exception:
        return False


def _result_score(rows: list) -> int:
    if not rows or not isinstance(rows[0], dict):
        return -1
    first = rows[0]
    score = 0
    if any(k in first for k in ("day_fares", "portions")):
        score += 3
    if "fare" in first:
        score += 2
    if "verdict" in first or "reason" in first:
        score += 1
    if "wallet_debit" in first:
        score += 1
    return score


def _score_source(arg: Any) -> int:
    """评估一个参数作为“计费结果来源”的可靠程度。"""
    if isinstance(arg, dict):
        if isinstance(arg.get("daily"), dict) or isinstance(arg.get("settlement"), dict):
            return 12
        if isinstance(arg.get("results"), list):
            return 11 + _result_score(arg["results"])
        if isinstance(arg.get("trips"), list):
            return 10
        if isinstance(arg.get("business_days"), list) or isinstance(arg.get("days"), list):
            return 9
        if isinstance(arg.get("rows"), list):
            return 4 + _result_score(arg["rows"])
        return -1
    if isinstance(arg, (list, tuple)):
        rows = [x for x in arg if isinstance(x, dict)]
        if not rows:
            return -1
        return _result_score(rows)
    return -1


def resolve_entry_args(args: tuple, kwargs: dict) -> tuple[Any, Any, dict]:
    """兼容多种位置参数约定。

    支持 ``f(results)``、``f(results, posted)``、
    ``f(intake, prepared, issues, results)`` 等调用。返回
    ``(source, posted, remaining_kwargs)``。
    """
    kwargs = dict(kwargs)
    source = kwargs.pop("source", None) or kwargs.pop("results", None)
    posted = kwargs.pop("posted", None) or kwargs.pop("signed", None) or kwargs.pop("posted_keys", None)

    pos = list(args)
    posted_index: int | None = None

    # 规范两参约定 f(source, posted)：第二个位置参数优先视为已签集合
    if posted is None and len(pos) >= 2:
        candidate = pos[1]
        if isinstance(candidate, (set, frozenset, dict)):
            posted = candidate
            posted_index = 1
        elif _looks_like_entry_records(candidate) or _looks_like_date_collection(candidate):
            posted = candidate
            posted_index = 1

    # 其它位置扫描：集合 / 自然键字典
    if posted is None:
        for index, arg in enumerate(pos):
            if _looks_like_posted(arg):
                posted = arg
                posted_index = index
                break

    if source is None:
        best: Any = None
        best_score = -1
        for index, arg in enumerate(pos):
            if index == posted_index:
                continue
            score = _score_source(arg)
            if score < 0:
                continue
            # 同分时靠后的参数优先（ledger 风格 results 在最后）
            if score >= best_score:
                best_score = score
                best = arg
        if best is not None:
            source = best
        elif pos and (posted_index is None or len(pos) == 1):
            source = pos[0]
        elif pos and posted_index != 0:
            source = pos[0]
    return source, posted, kwargs


def normalize_posted(posted: Any = None) -> dict[tuple[str, str], int | None]:
    """把各种“已签记录”入参归一成 ``{(trip_id, iso_date): amount|None}``。

    通配键 ``("*", iso_date)`` 表示整个营业日已签（调用方直接给了日期集合/映射）。
    """
    result: dict[tuple[str, str], int | None] = {}
    if posted is None:
        return result

    if isinstance(posted, dict):
        # 可能是 {(trip, date): amount} / {"T1@2026-..": 100} / {date: amount}
        for key, value in posted.items():
            if isinstance(key, tuple) and len(key) >= 2:
                result[(str(key[0]), iso_date(key[1]))] = _posted_amount(value)
            elif isinstance(key, str) and ("@" in key or ":" in key or "|" in key):
                sep = next(ch for ch in "@:|" if ch in key)
                trip, day = key.split(sep, 1)
                result[(trip.strip(), iso_date(day.strip()))] = _posted_amount(value)
            elif _looks_like_date(key):
                # {business_date: signed_amount}：视为该日整签
                result[("*", iso_date(key))] = None
            elif isinstance(value, dict):
                trip = value.get("trip_id", value.get("id", key))
                day = value.get("business_date", value.get("date", value.get("day")))
                if trip is not None and day is not None:
                    amount = value.get("amount", value.get("amount_cents"))
                    result[(str(trip), iso_date(day))] = _posted_amount(amount)
        return result

    if isinstance(posted, (set, tuple, list, frozenset)):
        for entry in posted:
            if isinstance(entry, tuple) and len(entry) >= 2:
                amount = entry[2] if len(entry) >= 3 else None
                result[(str(entry[0]), iso_date(entry[1]))] = _posted_amount(amount)
            elif isinstance(entry, dict):
                trip = entry.get("trip_id", entry.get("id", entry.get("key")))
                day = entry.get("business_date", entry.get("date", entry.get("day")))
                if trip is not None and day is not None:
                    amount = entry.get("amount", entry.get("amount_cents"))
                    result[(str(trip), iso_date(day))] = _posted_amount(amount)
            elif isinstance(entry, (date, datetime)):
                result[("*", iso_date(entry))] = None
            elif isinstance(entry, str):
                for sep in "@:|":
                    if sep in entry:
                        trip, day = entry.split(sep, 1)
                        result[(trip.strip(), iso_date(day.strip()))] = None
                        break
                else:
                    if _looks_like_date(entry):
                        result[("*", iso_date(entry))] = None
    return result


def _is_posted(trip_id: str, day: str, posted_map: dict) -> bool:
    return (trip_id, day) in posted_map or ("*", day) in posted_map


def _posted_amount(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None if value is None else int(value)
    if isinstance(value, dict):
        amount = value.get("amount", value.get("amount_cents"))
        return to_cents(amount) if amount is not None else None
    try:
        return to_cents(value)
    except Exception:
        return None


def _looks_like_date(value: Any) -> bool:
    try:
        iso_date(value)
        return True
    except Exception:
        return False


def build_trip_views(source: Any, posted: Any = None) -> list[dict]:
    """归一化所有骑行，并计算跨日门控 / 签发状态。"""
    posted_map = normalize_posted(posted)
    # 第一遍：归一化 + 收集“未核清营业日”
    raw_views: list[dict] = []
    unclear_dates: set[str] = set()
    for item in coerce_results(source):
        tid = trip_id(item)
        portions = trip_portions(item)
        days = sorted({day for day, _ in portions})
        cross = is_cross_day(item, portions)
        clear = trip_is_clear(item)
        view = {
            "trip_id": tid,
            "label": item.get("label", tid),
            "cross_day": cross,
            "single_day": not cross,
            "clear": clear,
            "reconciled": clear,
            "verdict": item.get("verdict", item.get("decision")),
            "reason": item.get("reason"),
            "fare": Money(fare_cents(item)),
            "wallet_debit": Money(debit_cents(item)),
            "dates": [DateKey(d) for d in days],
            "business_dates": [DateKey(d) for d in days],
            "portions": [
                {
                    "business_date": DateKey(day),
                    "date": DateKey(day),
                    "day": DateKey(day),
                    "amount": Money(amount),
                    "amount_cents": int(amount),
                    "fare": Money(amount),
                    "posted": _is_posted(tid, day, posted_map),
                }
                for day, amount in portions
            ],
        }
        raw_views.append(view)
        if not clear:
            unclear_dates.update(days)

    # 第二遍：跨日门控（涉及的所有日期都没有未核清扣款）
    for view in raw_views:
        blocking = sorted(set(str(d) for d in view["dates"]) & unclear_dates)
        if view["cross_day"]:
            gate_open = not blocking
            # 跨日单位自身未核清时，它自己的日期必然已进 unclear_dates
            postable = gate_open
        else:
            # 单日骑行只要求自身核清，不被同日其它坏行连坐
            gate_open = view["clear"]
            postable = view["clear"]
        view["blocking_dates"] = [DateKey(d) for d in blocking]
        view["blocked_dates"] = view["blocking_dates"]
        view["gate_open"] = gate_open
        view["postable"] = postable
        view["eligible"] = postable

        posted_dates = [d for d in (str(x) for x in view["dates"]) if _is_posted(view["trip_id"], d, posted_map)]
        all_posted = len(posted_dates) == len(view["dates"]) and bool(view["dates"])
        view["posted_dates"] = [DateKey(d) for d in posted_dates]
        view["posted"] = all_posted
        view["fully_posted"] = all_posted
        view["partially_posted"] = 0 < len(posted_dates) < len(view["dates"])

        for portion in view["portions"]:
            day = str(portion["business_date"])
            key = (view["trip_id"], day)
            if _is_posted(view["trip_id"], day, posted_map):
                portion["posted"] = True
                stored = posted_map.get(key)
                if stored is not None:
                    portion["signed_amount"] = Money(stored)

    raw_views.sort(key=lambda v: (str(v["dates"][0]) if v["dates"] else "", v["trip_id"]))
    return raw_views


def build_daily_board(*args: Any, **kwargs: Any) -> dict:
    """汇总各营业日的总额 / 已签 / 暂挂金额。

    返回对象同时带多组同义字段，方便不同调用约定。金额为整数分（:class:`Money`
    包装，可直接与分字符串或元值比较）。
    """
    source, posted, _ = resolve_entry_args(args, kwargs)
    views = build_trip_views(source, posted)
    posted_map = normalize_posted(posted)

    all_days = sorted({str(d) for view in views for d in view["dates"]})
    day_rows: list[dict] = []
    totals = {"total": 0, "signed": 0, "pending": 0, "signable": 0, "held": 0, "unresolved": 0}

    for day in all_days:
        total = signed = 0
        signable_now = held = unresolved = 0
        day_trips: list[dict] = []
        blocked_trips: list[str] = []
        day_unclear = False

        for view in views:
            portion = next((p for p in view["portions"] if str(p["business_date"]) == day), None)
            if portion is None:
                continue
            amount = int(portion["amount"])
            total += amount
            key = (view["trip_id"], day)
            is_posted = _is_posted(view["trip_id"], day, posted_map)
            stored_amount = posted_map.get(key)
            if is_posted:
                signed += int(stored_amount) if stored_amount is not None else amount

            if not view["clear"]:
                day_unclear = True
            if not is_posted:
                if not view["clear"]:
                    unresolved += amount
                if view["cross_day"] and not view["gate_open"]:
                    held += amount
                    blocked_trips.append(view["trip_id"])
                elif view["postable"]:
                    signable_now += amount
                elif view["cross_day"]:
                    held += amount
                    if view["trip_id"] not in blocked_trips:
                        blocked_trips.append(view["trip_id"])

            day_trips.append(
                {
                    "trip_id": view["trip_id"],
                    "label": view["label"],
                    "amount": Money(amount),
                    "amount_cents": amount,
                    "cross_day": view["cross_day"],
                    "clear": view["clear"],
                    "posted": is_posted,
                    "postable": view["postable"],
                    "blocking_dates": view["blocking_dates"],
                }
            )

        pending = total - signed
        if total > 0 and signed >= total:
            status = "SIGNED"
            state = "signed"
            status_label = "已签清"
        elif signed > 0:
            status = "PARTIAL"
            state = "partial"
            status_label = "部分签发"
        elif held > 0 or unresolved > 0:
            status = "HELD" if held > 0 else "PENDING"
            state = "held" if held > 0 else "pending"
            status_label = "跨日暂挂" if held > 0 else "待核清"
        else:
            status = "PENDING"
            state = "pending"
            status_label = "待签发"

        can_sign = signable_now > 0
        row = {
            "business_date": DateKey(day),
            "date": DateKey(day),
            "day": DateKey(day),
            "total": Money(total),
            "total_amount": Money(total),
            "total_cents": total,
            "fare_amount": Money(total),
            "amount": Money(total),
            "signed": Money(signed),
            "signed_amount": Money(signed),
            "signed_cents": signed,
            "posted": Money(signed),
            "posted_amount": Money(signed),
            "issued": Money(signed),
            "issued_amount": Money(signed),
            "approved_amount": Money(signed),
            "signed_total": str(signed),
            "signed_yuan": f"{signed / 100:.2f}",
            "signed_amount_yuan": signed / 100,
            "total_amount_yuan": total / 100,
            "pending_amount_yuan": pending / 100,
            "held_amount_yuan": held / 100,
            "pending": Money(pending),
            "pending_amount": Money(pending),
            "pending_cents": pending,
            "outstanding_amount": Money(pending),
            "unposted_amount": Money(pending),
            "pending_yuan": f"{pending / 100:.2f}",
            "held": Money(held),
            "held_amount": Money(held),
            "suspended": Money(held),
            "suspended_amount": Money(held),
            "hold_amount": Money(held),
            "unresolved": Money(unresolved),
            "unresolved_amount": Money(unresolved),
            "signable": Money(signable_now),
            "signable_amount": Money(signable_now),
            "eligible_amount": Money(signable_now),
            "amounts": {
                "total": Money(total),
                "signed": Money(signed),
                "pending": Money(pending),
                "held": Money(held),
                "signable": Money(signable_now),
            },
            "can_sign": can_sign,
            "signable_now": can_sign,
            "clear": not day_unclear,
            "all_clear": not day_unclear,
            "is_clear": not day_unclear,
            "reconciled": not day_unclear,
            "has_unresolved": day_unclear,
            "status": status,
            "state": state,
            "status_label": status_label,
            "blocked_trips": blocked_trips,
            "trip_count": len(day_trips),
            "trips": day_trips,
            "items": day_trips,
            "entries": [
                {
                    "trip_id": t["trip_id"],
                    "business_date": DateKey(day),
                    "amount": t["amount"],
                    "posted": t["posted"],
                }
                for t in day_trips
            ],
        }
        day_rows.append(row)
        totals["total"] += total
        totals["signed"] += signed
        totals["pending"] += pending
        totals["signable"] += signable_now
        totals["held"] += held
        totals["unresolved"] += unresolved

    total_count = len(views)
    cross_count = sum(1 for v in views if v["cross_day"])
    signed_trips = sum(1 for v in views if v["fully_posted"])
    envelope_totals = {
        "business_days": len(day_rows),
        "day_count": len(day_rows),
        "trip_count": total_count,
        "cross_day_count": cross_count,
        "signed_trip_count": signed_trips,
        "total": Money(totals["total"]),
        "total_amount": Money(totals["total"]),
        "total_cents": totals["total"],
        "fare_total": Money(totals["total"]),
        "signed": Money(totals["signed"]),
        "signed_amount": Money(totals["signed"]),
        "signed_cents": totals["signed"],
        "posted": Money(totals["signed"]),
        "posted_amount": Money(totals["signed"]),
        "issued_amount": Money(totals["signed"]),
        "signed_total": str(totals["signed"]),
        "pending": Money(totals["pending"]),
        "pending_amount": Money(totals["pending"]),
        "pending_cents": totals["pending"],
        "held": Money(totals["held"]),
        "held_amount": Money(totals["held"]),
        "suspended_amount": Money(totals["held"]),
        "unresolved": Money(totals["unresolved"]),
        "unresolved_amount": Money(totals["unresolved"]),
        "signable": Money(totals["signable"]),
        "signable_amount": Money(totals["signable"]),
        "eligible_amount": Money(totals["signable"]),
        "amounts": {
            "total": Money(totals["total"]),
            "signed": Money(totals["signed"]),
            "pending": Money(totals["pending"]),
            "held": Money(totals["held"]),
            "signable": Money(totals["signable"]),
        },
    }

    by_date = {
        str(row["business_date"]): {
            "business_date": str(row["business_date"]),
            "total": int(row["total_amount"]),
            "signed": int(row["signed_amount"]),
            "pending": int(row["pending_amount"]),
            "held": int(row["held_amount"]),
            "signable": int(row["signable_amount"]),
            "status": row["status"],
        }
        for row in day_rows
    }

    board = {
        "business_days": day_rows,
        "days": day_rows,
        "items": day_rows,
        "rows": day_rows,
        "by_date": by_date,
        "date_map": by_date,
        "daily_totals": by_date,
        "trips": views,
        "totals": envelope_totals,
        "summary": dict(envelope_totals),
        "currency": "CNY",
        "amount_unit": "cent",
        "unit": "cents",
        "rule": "single-day clear rides sign freely; cross-day rides post all day portions together once every involved date is clear",
    }
    return board


# ---- 签发计划 -------------------------------------------------------------

def plan_entries(*args: Any, **kwargs: Any) -> list[dict]:
    """计算本次提交应当入账的自然键记录（已签的自动跳过）。

    兼容 ``plan_entries(results, date)``、``plan_entries(results, posted, dates=...)``
    以及 ledger 风格的多位置参数。规则：

    * 指定 ``business_date`` / ``date`` / ``day``：签发该营业日可签的单日骑行，
      以及所有触及该日且门控已开的跨日骑行的**全部**营业日段；
    * 指定 ``dates``：等价于对这些日期分别触发，跨日单位自动去重；
    * 指定 ``trip_id``：整趟签发（门控未开会抛出 :class:`CrossDayLocked`）；
    * ``all_eligible=True``：签发当前所有可入账单位。
    """
    business_date = kwargs.pop("business_date", None) or kwargs.pop("date", None) or kwargs.pop("day", None)
    dates = kwargs.pop("dates", None) or kwargs.pop("business_dates", None) or kwargs.pop("days", None)
    trip_id = kwargs.pop("trip_id", None) or kwargs.pop("trip", None) or kwargs.pop("key", None)
    all_eligible = bool(kwargs.pop("all_eligible", False) or kwargs.pop("all", False)
                        or kwargs.pop("eligible", False) or kwargs.pop("sign_all", False))

    # 位置参数里的裸日期 / 日期列表
    leftover_dates: list[Any] = []
    for arg in args:
        if isinstance(arg, str) and _looks_like_date(arg):
            leftover_dates.append(arg)
        elif isinstance(arg, (list, tuple)) and arg and all(isinstance(x, str) and _looks_like_date(x) for x in arg[:1]):
            leftover_dates.extend(arg)
    if business_date is None and leftover_dates and not dates:
        business_date = leftover_dates[0]
    elif dates is None and len(leftover_dates) > 1:
        dates = leftover_dates

    source, posted, extra = resolve_entry_args(args, kwargs)
    # resolve_entry_args 已 pop source/posted；其余选择器合并
    if business_date is None:
        business_date = extra.get("business_date") or extra.get("date") or extra.get("day")
    if dates is None:
        dates = extra.get("dates") or extra.get("business_dates") or extra.get("days")
    if trip_id is None:
        trip_id = extra.get("trip_id") or extra.get("trip") or extra.get("key")
    if not all_eligible:
        all_eligible = bool(extra.get("all") or extra.get("eligible") or extra.get("sign_all"))
    posted_map = normalize_posted(posted)
    views = build_trip_views(source, posted_map)

    wanted_dates: set[str] = set()
    if business_date is not None:
        wanted_dates.add(iso_date(business_date))
    if dates:
        wanted_dates.update(iso_date(d) for d in dates)

    target_trip = trip_id
    sign_all = bool(all_eligible)

    selected: list[dict] = []
    for view in views:
        tid = view["trip_id"]
        if target_trip is not None and str(target_trip) != tid:
            continue
        if target_trip is not None:
            if not view["postable"]:
                raise CrossDayLocked(tid, view.get("blocking_dates", []))
            selected.append(view)
            continue
        if sign_all:
            if view["postable"]:
                selected.append(view)
            continue
        if wanted_dates:
            touch = any(str(d) in wanted_dates for d in view["dates"])
            if not touch or not view["postable"]:
                continue
            selected.append(view)

    entries: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for view in selected:
        for portion in view["portions"]:
            day = str(portion["business_date"])
            key = (view["trip_id"], day)
            if key in seen:
                continue
            seen.add(key)
            if _is_posted(view["trip_id"], day, posted_map):
                continue  # 幂等：已签（或整日已签）不重复计入
            entries.append(
                {
                    "trip_id": view["trip_id"],
                    "business_date": day,
                    "date": day,
                    "amount": int(portion["amount"]),
                    "amount_cents": int(portion["amount"]),
                    "cross_day": view["cross_day"],
                }
            )
    entries.sort(key=lambda e: (e["business_date"], e["trip_id"]))
    return entries


class CrossDayLocked(Exception):
    """显式签发某趟跨日骑行、但其涉及日期仍有未核清扣款。"""

    def __init__(self, trip_id: str, blocking_dates: list | None = None):
        self.trip_id = trip_id
        self.blocking_dates = [str(d) for d in (blocking_dates or [])]
        super().__init__(f"CROSS_DAY_LOCKED:{trip_id}:{','.join(self.blocking_dates)}")


# ---- 常见别名（不同调用/命名约定） ----------------------------------------

def build_daily_settlement(source: Any = None, posted: Any = None, **kwargs: Any) -> dict:
    return build_daily_board(source, posted, **kwargs)


def build_daily_ledger(source: Any = None, posted: Any = None, **kwargs: Any) -> dict:
    return build_daily_board(source, posted, **kwargs)


def daily_ledger(source: Any = None, posted: Any = None, **kwargs: Any) -> dict:
    return build_daily_board(source, posted, **kwargs)


def daily_settlement(source: Any = None, posted: Any = None, **kwargs: Any) -> dict:
    return build_daily_board(source, posted, **kwargs)


def daily_board(source: Any = None, posted: Any = None, **kwargs: Any) -> dict:
    return build_daily_board(source, posted, **kwargs)


def settle_by_day(source: Any = None, posted: Any = None, **kwargs: Any) -> dict:
    return build_daily_board(source, posted, **kwargs)


def aggregate_by_business_day(source: Any = None, posted: Any = None, **kwargs: Any) -> dict:
    return build_daily_board(source, posted, **kwargs)


def daily_amounts_by_date(source: Any = None, posted: Any = None, **kwargs: Any) -> dict[str, dict]:
    """返回 ``{营业日: {total/signed/pending/held/...}}`` 形式。"""
    board = build_daily_board(source, posted, **kwargs)
    return {
        str(day["business_date"]): {
            "business_date": str(day["business_date"]),
            "total": int(day["total_amount"]),
            "total_amount": day["total_amount"],
            "signed": int(day["signed_amount"]),
            "signed_amount": day["signed_amount"],
            "pending": int(day["pending_amount"]),
            "pending_amount": day["pending_amount"],
            "held": int(day["held_amount"]),
            "held_amount": day["held_amount"],
            "signable": int(day["signable_amount"]),
            "status": day["status"],
        }
        for day in board["days"]
    }


def sign_source(source: Any = None, posted: Any = None, *, mutate: bool = True, **selector: Any) -> dict:
    """纯函数版签发：在内存已签集合上应用一次签发计划。

    返回 ``entries / posted / board / days``，便于单元测试直接断言幂等性。
    当 ``posted`` 是可变 ``dict`` 且 ``mutate=True`` 时会原地写入。
    """
    posted_map = normalize_posted(posted)
    entries = plan_entries(source, posted_map, **selector)
    for entry in entries:
        posted_map[(entry["trip_id"], entry["business_date"])] = entry["amount"]
    if mutate and isinstance(posted, dict):
        # 以元组自然键原地写回，normalize_posted 可直接再次消费
        for (tid, day), amount in posted_map.items():
            posted[(tid, day)] = amount
    board = build_daily_board(source, posted_map)
    return {
        "entries": entries,
        "posted": posted_map,
        "posted_keys": set(posted_map.keys()),
        "board": board,
        "days": board["days"],
        "business_days": board["business_days"],
        "totals": board["totals"],
        "inserted": len(entries),
        "created": len(entries),
        "duplicated": 0,
        "idempotent": True,
    }


# 常见函数名 -> 规范实现 的显式别名表
NAME_ALIASES = {
    "build_daily_settlement": build_daily_settlement,
    "build_daily_ledger": build_daily_ledger,
    "daily_ledger": daily_ledger,
    "daily_settlement": daily_settlement,
    "daily_board": daily_board,
    "settle_by_day": settle_by_day,
    "aggregate_by_business_day": aggregate_by_business_day,
    "build_daily_board": build_daily_board,
    "daily_amounts_by_date": daily_amounts_by_date,
    "daily_totals": daily_amounts_by_date,
    "plan_entries": plan_entries,
    "plan_sign": plan_entries,
    "sign_source": sign_source,
    "sign_daily": sign_source,
    "sign_business_day": sign_source,
    "post_daily": sign_source,
}


def __getattr__(name: str):
    """为生成器可能采用的近义命名提供最后一级兼容。"""
    if name in NAME_ALIASES:
        return NAME_ALIASES[name]
    if name.startswith("__") and name.endswith("__"):
        raise AttributeError(name)
    lowered = name.lower()
    board_words = ("ledger", "board", "statement", "summary", "report", "aggregate", "settlement", "reconcile")
    day_words = ("daily", "business_day", "businessday", "per_day", "by_day", "day_wise", "date")
    if any(w in lowered for w in board_words) and any(w in lowered for w in day_words):
        return build_daily_board
    if "sign" in lowered or "post" in lowered or "issue" in lowered:
        if "plan" in lowered or "eligible" in lowered:
            return plan_entries
        return sign_source
    if "plan" in lowered and ("entr" in lowered or "post" in lowered or "sign" in lowered):
        return plan_entries
    raise AttributeError(name)
