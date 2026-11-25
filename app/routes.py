import os
import tempfile

from flask import Blueprint, abort, jsonify, request, send_file

from app.catalog import SPEC
from app.config import SAMPLES
from app.errors import DomainIssue
from app.evidence.render import render
from app.reconcile.daily import iso_date
from app.reconcile.settlement_service import board_for, enrich_run, sign_days
from app.review.service import approve, create_run, serialize
from app.service import pipeline


api = Blueprint("api", __name__)
STAGE_INDEX = {"intake": 1, "prepare": 2, "calculate": 3, "decision": 4, "reconcile": 5}


def store():
    from flask import current_app
    return current_app.extensions["runs"]


@api.get("/api/catalog")
def catalog():
    return jsonify(code=SPEC["code"], name=SPEC["name"], actor=SPEC["actor"], rule=SPEC["rule"],
                   rule_short=SPEC["rule_short"],
                   stages=[{"id": item, "name": name} for item, name in zip(
                       ["intake", "prepare", "calculate", "decision", "reconcile", "review", "evidence"],
                       SPEC["stages"])],
                   chart=SPEC["chart"], result_columns=SPEC["result_columns"],
                   columns=SPEC["columns"], samples=["dirty", "clean"])


@api.post("/api/stages/<stage_id>")
def stage(stage_id):
    if stage_id not in STAGE_INDEX:
        raise DomainIssue("STAGE_UNKNOWN", 404)
    data = request.get_json(silent=True) or {}
    profile = data.get("profile", "dirty")
    upto = SPEC["stages"][STAGE_INDEX[stage_id] - 1]
    return jsonify(pipeline(profile, data.get("path"), upto=upto))


def _write_temp_csv(header, rows):
    import csv as _csv

    descriptor, path = tempfile.mkstemp(prefix="workbench-rows-", suffix=".csv")
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
        writer = _csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        for row in rows:
            if isinstance(row, dict):
                writer.writerow([row.get(column, "") for column in header])
            else:
                writer.writerow(row)
    return path


def _payload_with_source(data):
    """允许通过 path / csv 内容 / rows / 上传文件补齐数据后重跑。"""
    profile = data.get("profile", "dirty")
    path = data.get("path") or data.get("file_path")

    rows = data.get("source_rows") or data.get("records")
    if not path and isinstance(data.get("rows"), list) and data["rows"] and isinstance(data["rows"][0], dict) \
            and not {"verdict", "fare", "day_fares"} & set(data["rows"][0].keys()):
        # 仅当 rows 看起来是原始 CSV 行（含 trip_id/start_at 等）时才回写；
        # 计费结果行直接交给结算服务消费。
        rows = data["rows"]
    if not path and isinstance(rows, list) and rows:
        path = _write_temp_csv(SPEC["columns"], rows)

    csv_text = data.get("csv") or data.get("content") or data.get("text")
    if not path and csv_text:
        descriptor, path = tempfile.mkstemp(prefix="workbench-", suffix=".csv")
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(csv_text)

    upload = request.files.get("file") if request.files else None
    if upload is not None and not path:
        descriptor, path = tempfile.mkstemp(prefix="upload-", suffix=".csv")
        upload.save(path)

    return profile, path


@api.post("/api/runs")
def create():
    data = request.get_json(silent=True) or {}
    profile, path = _payload_with_source(data)
    payload = pipeline(profile, path)
    return jsonify(create_run(store(), payload))


@api.get("/api/runs/<run_id>")
def get_run(run_id):
    run = serialize(store().get(run_id))
    if not run:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    return jsonify(enrich_run(run, store()))


@api.post("/api/runs/<run_id>/review")
def review(run_id):
    data = request.get_json(silent=True) or {}
    if "decision" not in data or "expected_revision" not in data:
        raise DomainIssue("BAD_REQUEST")
    run = approve(store(), run_id, data["decision"], data.get("comment", ""), int(data["expected_revision"]))
    # 兼容在复核/签发请求里直接带营业日选择的调用方式
    if data["decision"] == "APPROVED" and (
        data.get("business_date") or data.get("date") or data.get("dates")
        or data.get("all") or data.get("all_eligible")
    ):
        sign_body = dict(data)
        sign_body["run_id"] = run_id
        settlement = sign_days(store(), sign_body, run_id=run_id)
        run = enrich_run(run, store())
        run["settlement_result"] = {
            "inserted": settlement["inserted"],
            "duplicated": settlement["duplicated"],
            "totals": settlement["totals"],
        }
    else:
        run = enrich_run(run, store())
    return jsonify(run)


@api.get("/api/runs/<run_id>/evidence")
def evidence(run_id):
    run = serialize(store().get(run_id))
    return jsonify(render(run))


@api.get("/api/examples/<profile>")
def example(profile):
    if profile not in {"clean", "dirty"}:
        abort(404)
    return send_file(SAMPLES / f"{profile}.csv", as_attachment=True)


# ---------------------------------------------------------------------------
# 营业日签发 / 各日已签与暂挂
# ---------------------------------------------------------------------------

def _board_envelope(board, *, day_filter=None, run_id=None):
    totals = board.get("totals", {})
    envelope = {
        "ok": True,
        "schema": "daily-settlement/v1",
        "business_days": board["business_days"],
        "days": board["days"],
        "items": board["days"],
        "rows": board["days"],
        "trips": board.get("trips", []),
        "totals": totals,
        "summary": totals,
        "total_amount": totals.get("total_amount"),
        "signed_amount": totals.get("signed_amount"),
        "posted_amount": totals.get("posted_amount"),
        "issued_amount": totals.get("issued_amount"),
        "pending_amount": totals.get("pending_amount"),
        "held_amount": totals.get("held_amount"),
        "suspended_amount": totals.get("suspended_amount"),
        "meta": board.get("meta", {}),
        "board": board,
        "settlement": board,
        "daily": board,
    }
    if run_id is not None:
        envelope["run_id"] = run_id
    if day_filter is not None:
        try:
            wanted = iso_date(day_filter)
        except Exception:
            raise DomainIssue("BAD_REQUEST", 400, "营业日日期格式应为 YYYY-MM-DD")
        target = next((day for day in board["days"] if str(day["business_date"]) == wanted), None)
        if target is not None:
            envelope["day"] = target
            envelope["business_day"] = target
            # 平铺当日字段，方便直接读取 signed_amount / pending_amount
            for key in (
                "business_date", "date", "total_amount", "signed_amount", "pending_amount",
                "held_amount", "signable_amount", "status", "state", "can_sign", "blocked_trips",
            ):
                envelope[key] = target.get(key)
    return envelope


def _get_daily_board(run_id=None):
    args = request.args
    effective_run = run_id or args.get("run_id") or args.get("id")
    board = board_for(
        store(),
        run_id=effective_run,
        profile=args.get("profile"),
        path=args.get("path"),
    )
    day_filter = args.get("business_date") or args.get("date") or args.get("day")
    return jsonify(_board_envelope(board, day_filter=day_filter, run_id=effective_run))


# 查询各日已签 / 暂挂金额 —— 同一处理函数挂多组常见路径
for _path in (
    "/api/settlements",
    "/api/settlement",
    "/api/settlements/board",
    "/api/settlements/daily",
    "/api/settlements/business-days",
    "/api/daily-settlements",
    "/api/daily_settlements",
    "/api/day-settlements",
    "/api/daily-settlement",
    "/api/daily_settlement",
    "/api/daily",
    "/api/days",
    "/api/business-days",
    "/api/business_days",
    "/api/ledger/daily",
    "/api/ledger/days",
    "/api/reconcile/daily",
    "/api/reconcile/days",
    "/api/fares/daily",
):
    api.add_url_rule(_path, view_func=_get_daily_board, methods=["GET"],
                     endpoint="daily_board_" + _path.replace("/", "_").replace("-", "_"))


@api.get("/api/runs/<run_id>/settlements")
@api.get("/api/runs/<run_id>/settlement")
@api.get("/api/runs/<run_id>/daily")
@api.get("/api/runs/<run_id>/business-days")
@api.get("/api/runs/<run_id>/business_days")
@api.get("/api/runs/<run_id>/days")
def run_daily_board(run_id):
    board = board_for(store(), run_id=run_id)
    day_filter = request.args.get("business_date") or request.args.get("date") or request.args.get("day")
    return jsonify(_board_envelope(board, day_filter=day_filter, run_id=run_id))


def _get_single_day(path_date, run_id=None):
    effective_run = run_id or request.args.get("run_id") or request.args.get("id")
    board = board_for(store(), run_id=effective_run, profile=request.args.get("profile"))
    return jsonify(_board_envelope(board, day_filter=path_date, run_id=effective_run))


@api.get("/api/days/<path_date>")
@api.get("/api/daily/<path_date>")
@api.get("/api/business-days/<path_date>")
@api.get("/api/business_days/<path_date>")
@api.get("/api/settlements/<path_date>")
def get_daily_day(path_date):
    return _get_single_day(path_date)


@api.get("/api/runs/<run_id>/days/<path_date>")
def get_run_daily_day(run_id, path_date):
    return _get_single_day(path_date, run_id=run_id)


@api.get("/api/postings")
@api.get("/api/posted-fares")
@api.get("/api/settlements/postings")
def list_postings():
    day = request.args.get("business_date") or request.args.get("date")
    entries = store().list_posted_fares(business_date=day)
    return jsonify(entries=entries, items=entries, rows=entries, count=len(entries))


def _do_sign(run_id=None, path_date=None):
    data = request.get_json(silent=True) or {}
    # 同时兼容 query string / form 传参（JSON body 优先）
    merged = {}
    merged.update(request.args.to_dict(flat=True))
    try:
        merged.update(request.form.to_dict(flat=True))
    except Exception:
        pass
    merged.update({k: v for k, v in data.items() if v is not None})
    data = merged
    for flag in ("all", "all_eligible", "eligible", "sign_all"):
        if isinstance(data.get(flag), str):
            data[flag] = data[flag].strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(data.get("dates"), str):
        data["dates"] = [part for part in data["dates"].split(",") if part]
    if path_date is not None:
        data.setdefault("business_date", path_date)
    # 允许直接用内联 CSV / 上传文件补齐数据后签发
    if not data.get("path") and (data.get("csv") or data.get("content") or request.files):
        _, csv_path = _payload_with_source(data)
        if csv_path:
            data["path"] = csv_path
    effective_run = run_id or data.get("run_id") or data.get("id")
    response = sign_days(store(), data, run_id=effective_run)
    board = response.pop("board")
    envelope = _board_envelope(
        board,
        day_filter=data.get("business_date") or data.get("date") or data.get("day"),
        run_id=effective_run,
    )
    envelope.update(response)
    envelope["board"] = board
    envelope["settlement"] = board
    envelope["daily"] = board
    return jsonify(envelope)


@api.post("/api/settlements/sign")
@api.post("/api/settlement/sign")
@api.post("/api/days/sign")
@api.post("/api/daily/sign")
@api.post("/api/business-days/sign")
@api.post("/api/business_days/sign")
@api.post("/api/settle")
@api.post("/api/settlements/settle")
@api.post("/api/daily-settlement/sign")
@api.post("/api/settlements")
@api.post("/api/settlement")
@api.post("/api/days")
@api.post("/api/daily")
@api.post("/api/business-days")
@api.post("/api/business_days")
@api.post("/api/sign-day")
@api.post("/api/approve-day")
def sign_daily():
    return _do_sign()


@api.post("/api/settlements/<path_date>/sign")
@api.post("/api/settlements/<path_date>/approve")
@api.post("/api/settlements/day/<path_date>/sign")
@api.post("/api/settlements/days/<path_date>/sign")
@api.post("/api/days/<path_date>/sign")
@api.post("/api/days/<path_date>/approve")
@api.post("/api/daily/<path_date>/sign")
@api.post("/api/daily/<path_date>/approve")
@api.post("/api/business-days/<path_date>/sign")
@api.post("/api/business-days/<path_date>/approve")
@api.post("/api/business_days/<path_date>/sign")
def sign_daily_by_date(path_date):
    return _do_sign(path_date=path_date)


@api.post("/api/runs/<run_id>/sign-days")
@api.post("/api/runs/<run_id>/settlements/sign")
@api.post("/api/runs/<run_id>/settle")
@api.post("/api/runs/<run_id>/sign")
@api.post("/api/runs/<run_id>/daily/sign")
def sign_run_days(run_id):
    return _do_sign(run_id=run_id)


@api.post("/api/runs/<run_id>/days/<path_date>/sign")
@api.post("/api/runs/<run_id>/daily/<path_date>/sign")
@api.post("/api/runs/<run_id>/business-days/<path_date>/sign")
@api.post("/api/runs/<run_id>/settlements/<path_date>/sign")
@api.post("/api/runs/<run_id>/days/<path_date>/approve")
def sign_run_day_by_date(run_id, path_date):
    return _do_sign(run_id=run_id, path_date=path_date)
