import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=8)
        connection.row_factory = sqlite3.Row
        return connection

    def init(self):
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS runs(
              id TEXT PRIMARY KEY,
              request_key TEXT UNIQUE NOT NULL,
              created_at TEXT NOT NULL,
              profile TEXT NOT NULL,
              status TEXT NOT NULL,
              revision INTEGER NOT NULL,
              payload TEXT NOT NULL,
              comment TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS posted_fares(
              scope TEXT NOT NULL DEFAULT 'default',
              trip_id TEXT NOT NULL,
              business_date TEXT NOT NULL,
              amount_cents INTEGER NOT NULL,
              run_id TEXT,
              created_at TEXT NOT NULL,
              PRIMARY KEY (scope, trip_id, business_date)
            );
            CREATE INDEX IF NOT EXISTS idx_posted_fares_date
              ON posted_fares(scope, business_date);
            ''')

    def find_by_request(self, request_key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE request_key=?", (request_key,)).fetchone()
        return dict(row) if row else None

    def create(self, request_key, profile, payload):
        existing = self.find_by_request(request_key)
        if existing:
            return existing
        run_id = uuid.uuid4().hex[:12]
        with self.connect() as db:
            db.execute("INSERT INTO runs(id,request_key,created_at,profile,status,revision,payload) VALUES(?,?,?,?,?,?,?)",
                       (run_id, request_key, datetime.now(timezone.utc).isoformat(), profile, "DRAFT", 0,
                        json.dumps(payload, ensure_ascii=False)))
        return self.get(run_id)

    def get(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def review(self, run_id, decision, comment, expected_revision):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                return None, "missing"
            if row["revision"] != expected_revision:
                return None, "stale"
            db.execute("UPDATE runs SET status=?, revision=revision+1, comment=? WHERE id=?",
                       (decision, comment, run_id))
        return self.get(run_id), None

    def latest(self):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    # ---- 营业日入账（已签车费） -------------------------------------------

    def posted_fares(self, scope="default"):
        """返回 ``{(trip_id, business_date): amount_cents}``。"""
        with self.connect() as db:
            rows = db.execute(
                "SELECT trip_id, business_date, amount_cents FROM posted_fares WHERE scope=?",
                (scope,),
            ).fetchall()
        return {(row["trip_id"], row["business_date"]): int(row["amount_cents"]) for row in rows}

    def list_posted_fares(self, scope="default", business_date=None):
        query = "SELECT scope, trip_id, business_date, amount_cents, run_id, created_at FROM posted_fares WHERE scope=?"
        params = [scope]
        if business_date is not None:
            query += " AND business_date=?"
            params.append(str(business_date))
        query += " ORDER BY business_date, trip_id"
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def post_fares(self, entries, scope="default", run_id=None):
        """幂等地写入入账记录。

        ``entries`` 为 ``[{"trip_id", "business_date", "amount(_cents)"}]`` 或
        ``(trip_id, date, amount)`` 元组。依赖主键 ``(scope, trip_id, date)`` +
        ``BEGIN IMMEDIATE`` 保证“重跑 / 同时提交”都不会重复计入。

        返回 ``{"inserted": n, "duplicated": m, "entries": [...]}``。
        """
        normalized = []
        for entry in entries or []:
            if isinstance(entry, dict):
                tid = entry.get("trip_id", entry.get("id", entry.get("key")))
                day = entry.get("business_date", entry.get("date", entry.get("day")))
                amount = entry.get("amount_cents", entry.get("amount", 0))
                entry_run = entry.get("run_id", run_id)
            else:
                tid, day, amount = entry[0], entry[1], entry[2]
                entry_run = run_id
            if tid is None or day is None:
                continue
            from app.reconcile.daily import iso_date, to_cents

            normalized.append((scope, str(tid), iso_date(day), to_cents(amount), entry_run))

        inserted = 0
        detailed = []
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for scope_key, tid, day, amount, entry_run in normalized:
                cursor = db.execute(
                    """INSERT INTO posted_fares
                       (scope, trip_id, business_date, amount_cents, run_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(scope, trip_id, business_date) DO NOTHING""",
                    (scope_key, tid, day, amount, entry_run, now),
                )
                is_new = cursor.rowcount == 1
                inserted += int(is_new)
                row = db.execute(
                    "SELECT amount_cents FROM posted_fares WHERE scope=? AND trip_id=? AND business_date=?",
                    (scope_key, tid, day),
                ).fetchone()
                detailed.append(
                    {
                        "trip_id": tid,
                        "business_date": day,
                        "amount_cents": int(row["amount_cents"]) if row else amount,
                        "amount": int(row["amount_cents"]) if row else amount,
                        "inserted": is_new,
                        "duplicated": not is_new,
                    }
                )
        return {
            "inserted": inserted,
            "created": inserted,
            "duplicated": len(normalized) - inserted,
            "entries": detailed,
        }
