"""SQLite persistence for the authenticated Queue API relay."""

from __future__ import annotations

from contextlib import closing, contextmanager
import json
import math
import os
import re
import sqlite3
import time
from typing import Any, Callable
from uuid import uuid4

MAX_BODY = 1024 * 1024
MAX_BATCH = 8
MAX_LIST_LIMIT = 50
MAX_MEMORY_CHARS = 240
COMPANION_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9 _-]{0,62}[A-Za-z0-9_-])?$")
_PR_REF_RE = re.compile(
    r"(?:\bpr\s*:\s*[A-Za-z0-9._/-]+#\d+|"
    r"\b[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#\d+|"
    r"\bhttps?://github\.com/[^\s]+/pull/\d+(?:/[^\s]*)?|"
    r"\bpull/\d+\b|\bPR\s*#?\d+\b|#\d+\b)",
    re.IGNORECASE,
)
_COMMIT_REF_RE = re.compile(
    r"(?:@?sha256:[0-9a-f]{7,64}|"
    r"@[0-9a-f]{7,40}\b|"
    r"\b(?:commit|rev(?:ision)?)\s*[:=]?\s*[0-9a-f]{7,40}\b|"
    r"\b[0-9a-f]{7,40}\b)",
    re.IGNORECASE,
)
_REVIEW_HISTORY_RE = re.compile(
    r"\b(?:verdict|reviewed_revision|artifact_revision|changes_requested|review_findings?|"
    r"findings?)\s*[:=].*$",
    re.IGNORECASE,
)
_CREDENTIAL_RE = re.compile(
    r"(?:Bearer\s+\S+|gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|"
    r"(?:sk|pk|rk)-[A-Za-z0-9_-]{10,}|AIza[A-Za-z0-9_-]{12,})",
    re.IGNORECASE,
)
TERMINAL = {"done", "blocked", "failed"}
WORKER_STATES = {"running", "idle", "stopped", "unknown"}
TRANSITIONS = {
    "queued": {"queued", "running", "held", "manual_started", "awaiting", *TERMINAL},
    "running": {"running", "awaiting", *TERMINAL},
    "manual": {"manual", "manual_ready", "manual_started", "running", "held", "awaiting", "done", "blocked"},
    "manual_ready": {"manual_ready", "manual_started", "held", "awaiting", "done", "blocked"},
    "manual_started": {"manual_started", "awaiting", "done", "blocked"},
    "awaiting": {"awaiting", "queued", "manual", "manual_ready", "running", "manual_started", "held", *TERMINAL},
    "held": {"held", "queued", "manual", "done", "blocked"},
}
WORKER_FIELDS = {
    "claim": {"limit"},
    "ack": {"bridge_id", "lease_id", "job_id"},
    "status": {"bridge_id", "job_id", "snapshot"},
    "heartbeat": {"worker_state"},
    "uncertain": {"limit"},
    "reconcile": {"bridge_id", "resolution"},
}
WORKER_OPTIONAL_FIELDS = {
    "heartbeat": {"queued_local", "running_local"},
    "reconcile": {"job_id"},
}


class Conflict(ValueError):
    """An existing submission, lease, or binding conflicts with the update."""


def _json(value: Any) -> str:
    try:
        encoded = json.dumps(value, allow_nan=False, sort_keys=True)
    except (ValueError, TypeError, RecursionError):
        raise ValueError("Invalid relay JSON") from None
    if len(encoded.encode("utf-8")) > MAX_BODY:
        raise ValueError("Relay JSON exceeds size limit")
    return encoded


def _string(value: Any, limit: int = 128) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        raise ValueError("Invalid relay string")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("Invalid relay string") from None
    return value


def _companion_label(value: Any, field_name: str) -> str:
    label = _string(value, 64)
    if not COMPANION_PATTERN.fullmatch(label):
        raise ValueError(f"Invalid {field_name} label")
    return label


def _worker_job_id(value: Any) -> None:
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError("Invalid local job ID")


def _normalize_task_request(arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    allowed = {
        "request_id",
        "task",
        "backend",
        "model",
        "tier",
        "codex_fallback_authorized",
        "priority",
        "effort",
        "requester",
        "executor",
        "companion",
        "task_kind",
        "delegation_mode",
    }
    if arguments.keys() - allowed:
        raise ValueError("Unexpected queue argument")

    task = _string(arguments.get("task"), 16384)
    if not task.strip() or len(task.encode("utf-8")) > 16384:
        raise ValueError("Task must contain 1 to 16384 UTF-8 bytes")

    backend = arguments.get("backend", "codex")
    if backend not in ("codex", "agy", "auto"):
        raise ValueError("Unknown backend")

    raw_model = arguments.get("model")
    if raw_model is None or raw_model == "":
        model = None
    else:
        model = _string(raw_model, 128)

    priority = arguments.get("priority", 0)
    if type(priority) is not int or not -100 <= priority <= 100:
        raise ValueError("Invalid priority")

    codex_fallback = arguments.get("codex_fallback_authorized", False)
    if codex_fallback is None:
        codex_fallback = False
    if type(codex_fallback) is not bool:
        raise ValueError("codex_fallback_authorized must be a boolean")

    raw_delegation = arguments.get("delegation_mode", "queue_only")
    if raw_delegation is None:
        delegation_mode = "queue_only"
    else:
        delegation_mode = _string(raw_delegation, 32)
        if delegation_mode != "queue_only":
            raise ValueError("Only queue_only delegation is supported")

    payload: dict[str, Any] = {
        "task": task,
        "backend": backend,
        "model": model,
        "priority": priority,
        "codex_fallback_authorized": codex_fallback,
        "delegation_mode": delegation_mode,
    }

    if "tier" in arguments and arguments["tier"] is not None:
        tier = _string(arguments["tier"], 16).strip().lower()
        if tier == "medium":
            tier = "mid"
        if tier not in ("cheap", "mid", "expensive"):
            raise ValueError("Invalid tier")
        payload["tier"] = tier

    for key in ("requester", "executor", "companion"):
        if key in arguments and arguments[key] is not None:
            payload[key] = _companion_label(arguments[key], key)

    if (
        "executor" in payload
        and "companion" in payload
        and payload["executor"] != payload["companion"]
    ):
        raise ValueError("Executor and companion conflict; specify one executing companion")

    if "effort" in arguments and arguments["effort"] is not None:
        effort = _string(arguments["effort"], 32).strip().lower()
        if effort not in ("low", "medium", "high", "xhigh", "max"):
            raise ValueError("Invalid reasoning effort")
        payload["effort"] = effort

    if "task_kind" in arguments and arguments["task_kind"] is not None:
        task_kind = _string(arguments["task_kind"], 32)
        if task_kind not in ("implementation", "review", "research", "maintenance", "general"):
            raise ValueError("Invalid task kind")
        payload["task_kind"] = task_kind

    return _json(payload), payload


def _validate_snapshot(snapshot: Any, job_id: int) -> None:
    if not isinstance(snapshot, dict) or snapshot.keys() != {
        "id",
        "priority",
        "state",
        "attempts",
        "result",
        "finished",
    }:
        raise ValueError("Invalid job snapshot")
    _worker_job_id(snapshot["id"])
    state = snapshot["state"]
    attempts = snapshot["attempts"]
    priority = snapshot["priority"]
    finished = snapshot["finished"]
    if (
        snapshot["id"] != job_id
        or not isinstance(state, str)
        or state not in TERMINAL | TRANSITIONS.keys()
        or type(attempts) is not int
        or not 0 <= attempts < 2**63
        or type(priority) is not int
        or not -100 <= priority <= 100
        or (
            finished is not None
            and (type(finished) not in (int, float) or not 0 <= finished < math.inf)
        )
    ):
        raise ValueError("Invalid job snapshot")


class QueueRelayStore:
    """Durable SQLite relay store scoped by verified owner and worker route."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        lease_seconds: float = 60.0,
        stale_seconds: float = 120.0,
        clock: Callable[[], float] = time.time,
    ):
        if any(not 0 < value < math.inf for value in (lease_seconds, stale_seconds)):
            raise ValueError("Invalid relay time limits")
        self.path = os.fspath(path)
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.lease_seconds = lease_seconds
        self.stale_seconds = stale_seconds
        self.clock = clock
        with closing(sqlite3.connect(self.path, timeout=5.0)) as init_db:
            init_db.execute("PRAGMA busy_timeout=5000")
            init_db.execute("PRAGMA journal_mode=WAL")
        with self._transaction() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS relay_requests (
                    seq INTEGER PRIMARY KEY,
                    job_id TEXT NOT NULL UNIQUE,
                    owner TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    companion TEXT,
                    requester TEXT,
                    executor TEXT,
                    route TEXT NOT NULL,
                    bridge_id TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    delivery TEXT NOT NULL DEFAULT 'pending',
                    lease_id TEXT,
                    deadline REAL,
                    worker_job_id INTEGER,
                    snapshot TEXT,
                    terminal INTEGER NOT NULL DEFAULT 0,
                    observed_at REAL,
                    offered_at INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    UNIQUE (owner, request_id),
                    UNIQUE (route, worker_job_id)
                )
                """
            )
            db.execute(
                """
                CREATE INDEX IF NOT EXISTS relay_route_delivery
                ON relay_requests (route, delivery, seq)
                """
            )
            db.execute(
                """
                CREATE INDEX IF NOT EXISTS relay_owner_job_id
                ON relay_requests (owner, job_id)
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS relay_heartbeats (
                    route TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    worker_state TEXT NOT NULL,
                    queued_local INTEGER,
                    running_local INTEGER,
                    observed_at REAL NOT NULL
                )
                """
            )

    @contextmanager
    def _transaction(self):
        with closing(sqlite3.connect(self.path, timeout=5.0)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("BEGIN IMMEDIATE")
            yield db

    def _receipt(
        self,
        row: sqlite3.Row,
        now: float,
        *,
        worker_heartbeat_at: float | None = None,
    ) -> dict[str, Any]:
        delivery = row["delivery"]
        if delivery == "leased" and row["deadline"] <= now:
            delivery = "delivery_uncertain"
        snapshot = json.loads(row["snapshot"]) if row["snapshot"] else None
        observed = row["observed_at"]
        state = snapshot["state"] if snapshot else None
        stale = (
            observed is not None
            and state not in TERMINAL
            and now - observed >= self.stale_seconds
        )
        if stale and state in ("running", "manual_started"):
            state = None
            snapshot = None
        payload = json.loads(row["payload"])
        task = payload.get("task", "")
        title = next((line.strip() for line in task.splitlines() if line.strip()), "")[:240]
        routing = {
            key: payload[key]
            for key in ("backend", "model", "tier", "effort", "priority", "task_kind", "delegation_mode")
            if key in payload
        }
        routing["queue_route"] = row["route"]
        return {
            "job_id": row["job_id"],
            "request_id": row["request_id"],
            "delivery_state": delivery,
            "state": state,
            "worker_job_id": row["worker_job_id"],
            "companion": row["companion"],
            "requester": row["requester"],
            "executor": row["executor"],
            "snapshot": snapshot,
            "receipt": snapshot["result"] if snapshot else None,
            "observed_at": observed,
            "worker_heartbeat_at": worker_heartbeat_at,
            "stale": stale,
            "delivery_action": (
                "Check worker reconciliation and verify the original bridge ID against local intake before retrying."
                if delivery == "delivery_uncertain" else None
            ),
            "title": title,
            "instructions": task,
            "routing": routing,
            "created_at": row["created_at"],
        }

    def _route_heartbeat_at(self, db: sqlite3.Connection, route: str) -> float | None:
        hb = db.execute(
            "SELECT observed_at FROM relay_heartbeats WHERE route = ?",
            (route,),
        ).fetchone()
        return hb["observed_at"] if hb is not None else None

    def principal(
        self,
        owner: str,
        route: str,
        operation: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Dispatch an owner MCP queue tool using verified caller identity and route."""
        _string(owner)
        _string(route)
        if not isinstance(arguments, dict):
            raise ValueError("Invalid owner arguments")

        if operation == "queue_task":
            request_id = _string(arguments.get("request_id"))
            if not request_id.strip():
                raise ValueError("request_id must not be blank")
            payload_json, payload_dict = _normalize_task_request(arguments)
            with self._transaction() as db:
                now = self.clock()
                lookup = "SELECT * FROM relay_requests WHERE owner = ? AND request_id = ?"
                key = (owner, request_id)
                row = db.execute(lookup, key).fetchone()
                if row is None:
                    public_job_id = f"qjob_{uuid4().hex[:16]}"
                    bridge_id = uuid4().hex
                    db.execute(
                        """
                        INSERT INTO relay_requests (
                            job_id, owner, request_id, companion, requester, executor,
                            route, bridge_id, payload, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            public_job_id,
                            owner,
                            request_id,
                            payload_dict.get("companion"),
                            payload_dict.get("requester"),
                            payload_dict.get("executor"),
                            route,
                            bridge_id,
                            payload_json,
                            now,
                        ),
                    )
                    row = db.execute(lookup, key).fetchone()
                elif row["payload"] != payload_json:
                    raise Conflict("Request ID already belongs to a different task")
                return self._receipt(
                    row,
                    now,
                    worker_heartbeat_at=self._route_heartbeat_at(db, row["route"]),
                )

        if operation == "list_jobs":
            allowed = {"limit", "offset", "cursor", "state", "delivery_state", "backend", "requester", "executor", "companion", "task_kind"}
            if arguments.keys() - allowed:
                raise ValueError("Unexpected list_jobs argument")
            limit = arguments.get("limit", 20)
            offset = arguments.get("offset", 0)
            if type(limit) is not int or not 1 <= limit <= MAX_LIST_LIMIT:
                raise ValueError(f"limit must be between 1 and {MAX_LIST_LIMIT}")
            if type(offset) is not int or not 0 <= offset < 2**31:
                raise ValueError("offset must be a nonnegative integer")
            cursor = arguments.get("cursor")
            if cursor is not None and (type(cursor) is not int or not 0 < cursor < 2**63):
                raise ValueError("cursor must be a positive sequence number")
            if cursor is not None and offset:
                raise ValueError("cursor cannot be combined with a nonzero offset")
            filters: list[str] = []
            values: list[Any] = [owner]
            for key, column in (("requester", "requester"), ("executor", "executor"), ("companion", "companion")):
                value = arguments.get(key)
                if value is not None:
                    filters.append(f"{column} = ?")
                    values.append(_companion_label(value, key))
            task_kind = arguments.get("task_kind")
            if task_kind is not None:
                task_kind = _string(task_kind, 32)
                if task_kind not in ("implementation", "review", "research", "maintenance", "general"):
                    raise ValueError("Invalid task_kind")
                filters.append("json_extract(payload, '$.task_kind') = ?")
                values.append(task_kind)
            backend = arguments.get("backend")
            if backend is not None:
                if backend not in ("codex", "agy", "auto"):
                    raise ValueError("Invalid backend")
                filters.append("json_extract(payload, '$.backend') = ?")
                values.append(backend)
            delivery_state = arguments.get("delivery_state")
            if delivery_state is not None:
                if delivery_state not in ("pending", "leased", "delivered", "delivery_uncertain"):
                    raise ValueError("Invalid delivery_state")
                filters.append("delivery = ?")
                values.append(delivery_state)
            state = arguments.get("state")
            if state is not None:
                if not isinstance(state, str) or state not in TRANSITIONS.keys() | TERMINAL:
                    raise ValueError("Invalid state")
                filters.append("json_extract(snapshot, '$.state') = ?")
                values.append(state)
            count_where = " AND ".join(["owner = ?", *filters])
            if cursor is not None:
                filters.append("seq < ?")
                values.append(cursor)
            where = " AND ".join(["owner = ?", *filters])
            with self._transaction() as db:
                total = db.execute(
                    f"SELECT COUNT(*) FROM relay_requests WHERE {count_where}", values[:len(values) - (1 if cursor is not None else 0)]
                ).fetchone()[0]
                rows = db.execute(
                    f"SELECT * FROM relay_requests WHERE {where} ORDER BY seq DESC LIMIT ? OFFSET ?",
                    [*values, limit + 1, offset],
                ).fetchall()
                has_more = len(rows) > limit
                rows = rows[:limit]
                now = self.clock()
                items = [
                    self._receipt(row, now, worker_heartbeat_at=self._route_heartbeat_at(db, row["route"]))
                    for row in rows
                ]
            return {
                "items": items,
                "total": total,
                "limit": limit,
                "offset": offset,
                "next_cursor": rows[-1]["seq"] if rows and has_more else None,
            }

        if operation == "get_task":
            if arguments.keys() - {"job_id", "request_id", "worker_job_id"}:
                raise ValueError("Unexpected get_task argument")
            job_id = arguments.get("job_id")
            request_id = arguments.get("request_id")
            worker_job_id = arguments.get("worker_job_id")
            if worker_job_id is not None and (job_id is not None or request_id is not None):
                raise ValueError("worker_job_id cannot be combined with job_id or request_id")
            if job_id is None and request_id is None and worker_job_id is None:
                raise ValueError("Specify job_id, request_id, or worker_job_id")
            if job_id is not None:
                job_id = _string(job_id)
            if request_id is not None:
                request_id = _string(request_id)
            if worker_job_id is not None and (type(worker_job_id) is not int or not 0 < worker_job_id < 2**63):
                raise ValueError("Invalid local worker job ID")
            with self._transaction() as db:
                if job_id is not None:
                    if request_id is not None:
                        row = db.execute(
                            "SELECT * FROM relay_requests WHERE owner = ? AND job_id = ? AND request_id = ?",
                            (owner, job_id, request_id),
                        ).fetchone()
                    else:
                        row = db.execute(
                            "SELECT * FROM relay_requests WHERE owner = ? AND job_id = ?",
                            (owner, job_id),
                        ).fetchone()
                elif request_id is not None:
                    row = db.execute(
                        "SELECT * FROM relay_requests WHERE owner = ? AND request_id = ?",
                        (owner, request_id),
                    ).fetchone()
                else:
                    matches = db.execute(
                        "SELECT * FROM relay_requests WHERE owner = ? AND worker_job_id = ? ORDER BY seq DESC LIMIT 2",
                        (owner, worker_job_id),
                    ).fetchall()
                    if len(matches) > 1:
                        raise ValueError("Local worker job ID is ambiguous; use job_id")
                    row = matches[0] if matches else None
                if row is None:
                    raise LookupError("Task was not found")
                return self._receipt(
                    row,
                    self.clock(),
                    worker_heartbeat_at=self._route_heartbeat_at(db, row["route"]),
                )

        raise ValueError("Invalid owner operation")

    def worker(
        self,
        route: str,
        operation: str,
        payload: dict[str, Any],
        *,
        worker_id: str = "worker",
    ) -> dict[str, Any]:
        """Dispatch a v1 JSON request using a verified worker route."""
        _string(route)
        _string(worker_id)
        if operation not in WORKER_FIELDS or not isinstance(payload, dict):
            raise ValueError("Invalid worker request")
        required_keys = WORKER_FIELDS[operation] | {"version"}
        optional_keys = WORKER_OPTIONAL_FIELDS.get(operation, set())
        if (
            not (required_keys <= payload.keys() <= (required_keys | optional_keys))
            or type(payload["version"]) is not int
            or payload["version"] != 1
        ):
            raise ValueError("Invalid worker request")
        _json(payload)
        with self._transaction() as db:
            now = self.clock()
            if operation == "claim":
                return self._claim(db, route, payload["limit"], now)
            if operation == "uncertain":
                return self._uncertain(db, route, payload["limit"], now)
            if operation == "heartbeat":
                return self._heartbeat(db, route, worker_id, payload, now)
            bridge_id = _string(payload["bridge_id"])
            if operation == "reconcile":
                resolution = _string(payload["resolution"], 32)
                job_id = payload.get("job_id")
                if job_id is not None:
                    _worker_job_id(job_id)
                row = db.execute(
                    "SELECT * FROM relay_requests WHERE route = ? AND bridge_id = ?",
                    (route, bridge_id),
                ).fetchone()
                if row is None:
                    raise LookupError("Binding was not found")
                self._reconcile(db, row, resolution, job_id, now)
                return {"version": 1, "ok": True}
            job_id = payload["job_id"]
            _worker_job_id(job_id)
            row = db.execute(
                "SELECT * FROM relay_requests WHERE route = ? AND bridge_id = ?",
                (route, bridge_id),
            ).fetchone()
            if row is None:
                raise LookupError("Binding was not found")
            if operation == "ack":
                self._ack(db, row, _string(payload["lease_id"]), job_id, now)
            else:
                self._status(db, row, job_id, payload["snapshot"], now)
            return {"version": 1, "ok": True}

    def _heartbeat(
        self,
        db: sqlite3.Connection,
        route: str,
        worker_id: str,
        payload: dict[str, Any],
        now: float,
    ) -> dict[str, Any]:
        worker_state = payload["worker_state"]
        if not isinstance(worker_state, str) or worker_state not in WORKER_STATES:
            raise ValueError("Invalid worker_state")
        queued_local = payload.get("queued_local")
        running_local = payload.get("running_local")
        for count in (queued_local, running_local):
            if count is not None and (type(count) is not int or not 0 <= count < 2**31):
                raise ValueError("Invalid local queue counter")
        db.execute(
            """
            INSERT INTO relay_heartbeats (
                route, worker_id, worker_state, queued_local, running_local, observed_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(route) DO UPDATE SET
                worker_id = excluded.worker_id,
                worker_state = excluded.worker_state,
                queued_local = excluded.queued_local,
                running_local = excluded.running_local,
                observed_at = excluded.observed_at
            """,
            (route, worker_id, worker_state, queued_local, running_local, now),
        )
        return {"version": 1, "ok": True, "observed_at": now}

    def _claim(self, db: sqlite3.Connection, route: str, limit: Any, now: float) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= MAX_BATCH:
            raise ValueError("Invalid claim limit")
        db.execute(
            """
            UPDATE relay_requests SET delivery = 'delivery_uncertain'
            WHERE route = ? AND delivery = 'leased' AND deadline <= ?
            """,
            (route, now),
        )
        claims = []
        for row in db.execute(
            """
            SELECT seq, bridge_id, payload FROM relay_requests
            WHERE route = ? AND delivery = 'pending' ORDER BY seq LIMIT ?
            """,
            (route, limit),
        ).fetchall():
            lease_id = uuid4().hex
            deadline = now + self.lease_seconds
            db.execute(
                """
                UPDATE relay_requests SET delivery = 'leased', lease_id = ?, deadline = ?
                WHERE seq = ?
                """,
                (lease_id, deadline, row["seq"]),
            )
            claims.append(
                {
                    "bridge_id": row["bridge_id"],
                    "lease_id": lease_id,
                    "deadline": deadline,
                    "request": json.loads(row["payload"]),
                }
            )
        bindings = db.execute(
            """
            SELECT seq, bridge_id, worker_job_id FROM relay_requests
            WHERE route = ? AND delivery = 'delivered'
              AND terminal = 0
            ORDER BY offered_at, seq LIMIT ?
            """,
            (route, limit),
        ).fetchall()
        if bindings:
            next_offer = db.execute(
                "SELECT MAX(offered_at) + 1 FROM relay_requests WHERE route = ?",
                (route,),
            ).fetchone()[0]
            db.executemany(
                "UPDATE relay_requests SET offered_at = ? WHERE seq = ?",
                [(next_offer, row["seq"]) for row in bindings],
            )
        return {
            "version": 1,
            "claims": claims,
            "bindings": [
                {"bridge_id": row["bridge_id"], "job_id": row["worker_job_id"]}
                for row in bindings
            ],
        }

    def _ack(
        self,
        db: sqlite3.Connection,
        row: sqlite3.Row,
        lease_id: str,
        job_id: int,
        now: float,
    ) -> None:
        if row["lease_id"] != lease_id:
            raise Conflict("Lease mismatch")
        if row["delivery"] == "delivered" and row["worker_job_id"] == job_id:
            return
        if row["delivery"] != "leased" or row["deadline"] <= now:
            raise Conflict("Lease is no longer active")
        try:
            db.execute(
                """
                UPDATE relay_requests SET delivery = 'delivered', worker_job_id = ?
                WHERE seq = ?
                """,
                (job_id, row["seq"]),
            )
        except sqlite3.IntegrityError:
            raise Conflict("Local job ID already bound on this route") from None

    def _status(
        self,
        db: sqlite3.Connection,
        row: sqlite3.Row,
        job_id: int,
        snapshot: Any,
        now: float,
    ) -> None:
        if row["delivery"] != "delivered" or row["worker_job_id"] != job_id:
            raise Conflict("Local job binding mismatch")
        _validate_snapshot(snapshot, job_id)
        encoded = _json(snapshot)
        if row["snapshot"] is not None:
            prior = json.loads(row["snapshot"])
            if prior["state"] in TERMINAL:
                if row["snapshot"] == encoded:
                    return
                raise Conflict("Terminal snapshot already recorded")
            if (
                snapshot["state"] not in TRANSITIONS[prior["state"]]
                or snapshot["attempts"] < prior["attempts"]
            ):
                raise Conflict("Stale or invalid job transition")
        db.execute(
            """
            UPDATE relay_requests SET snapshot = ?, observed_at = ?, terminal = ?
            WHERE seq = ?
            """,
            (encoded, now, int(snapshot["state"] in TERMINAL), row["seq"]),
        )

    def _uncertain(self, db: sqlite3.Connection, route: str, limit: Any, now: float) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= MAX_BATCH:
            raise ValueError("Invalid uncertain limit")
        db.execute(
            """
            UPDATE relay_requests SET delivery = 'delivery_uncertain'
            WHERE route = ? AND delivery = 'leased' AND deadline <= ?
            """,
            (route, now),
        )
        rows = db.execute(
            """
            SELECT bridge_id, lease_id, deadline, payload FROM relay_requests
            WHERE route = ? AND delivery = 'delivery_uncertain'
            ORDER BY seq LIMIT ?
            """,
            (route, limit),
        ).fetchall()
        return {
            "version": 1,
            "uncertain": [
                {
                    "bridge_id": row["bridge_id"],
                    "lease_id": row["lease_id"],
                    "deadline": row["deadline"],
                    "request": json.loads(row["payload"]),
                }
                for row in rows
            ],
        }

    def _reconcile(
        self,
        db: sqlite3.Connection,
        row: sqlite3.Row,
        resolution: str,
        job_id: int | None,
        now: float,
    ) -> None:
        if resolution not in ("bind_existing", "requeue_absent"):
            raise ValueError("Unsupported reconciliation resolution")
        if resolution == "bind_existing":
            if job_id is None:
                raise ValueError("bind_existing requires local job_id")
            if row["delivery"] == "delivered" and row["worker_job_id"] == job_id:
                return
            if row["delivery"] != "delivery_uncertain":
                raise Conflict("Request is not awaiting reconciliation")
            try:
                db.execute(
                    """
                    UPDATE relay_requests
                    SET delivery = 'delivered', worker_job_id = ?, observed_at = COALESCE(observed_at, ?)
                    WHERE seq = ?
                    """,
                    (job_id, now, row["seq"]),
                )
            except sqlite3.IntegrityError:
                raise Conflict("Local job ID already bound on this route") from None
            return

        if row["delivery"] == "pending" and row["worker_job_id"] is None:
            return
        if row["delivery"] != "delivery_uncertain" or row["worker_job_id"] is not None:
            raise Conflict("Request cannot be requeued from current delivery state")
        db.execute(
            """
            UPDATE relay_requests
            SET delivery = 'pending', lease_id = NULL, deadline = NULL
            WHERE seq = ?
            """,
            (row["seq"],),
        )

    def terminal_participation_specs(self, route: str, bridge_id: str) -> list[dict[str, Any]]:
        """Build one short participated-in-task memory spec per companion for a terminal relay job."""
        _string(route)
        _string(bridge_id)
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM relay_requests WHERE route = ? AND bridge_id = ?",
                (route, bridge_id),
            ).fetchone()
        if row is None or not row["snapshot"]:
            return []
        snapshot = json.loads(row["snapshot"])
        state = snapshot.get("state") if isinstance(snapshot, dict) else None
        if state not in TERMINAL:
            return []
        payload = json.loads(row["payload"]) if row["payload"] else {}
        return build_task_participation_memories(row, payload, snapshot)


def sanitize_task_memory_summary(text: str, max_len: int = 140) -> str:
    """Strip exact PR, commit, review-history, and credential tokens from task summaries."""
    if not isinstance(text, str):
        return ""
    cleaned = _CREDENTIAL_RE.sub("[redacted]", text)
    cleaned = _REVIEW_HISTORY_RE.sub("", cleaned)
    cleaned = _PR_REF_RE.sub("", cleaned)
    cleaned = _COMMIT_REF_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,:;-")
    if len(cleaned) > max_len:
        return cleaned[: max_len - 3].rstrip() + "..."
    return cleaned


def build_task_participation_memories(
    row: sqlite3.Row | dict[str, Any],
    payload: dict[str, Any],
    snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build at most one short participated-in-task memory per companion and task."""
    state = snapshot.get("state")
    if state not in TERMINAL:
        return []
    result = snapshot.get("result") if isinstance(snapshot.get("result"), dict) else {}
    contract = result.get("result_contract") if isinstance(result.get("result_contract"), dict) else {}
    task_text = payload.get("task") or ""
    first_line = next((line.strip() for line in task_text.splitlines() if line.strip()), "")
    short_goal = sanitize_task_memory_summary(first_line)
    outcome_kind = result.get("outcome_kind")
    if outcome_kind == "missing_receipt" or result.get("missing_receipt"):
        outcome_label = "missing receipt"
    elif outcome_kind == "in_task_blocker" or state == "blocked":
        outcome_label = "blocked"
    else:
        outcome_label = str(state)

    task_key = str(row["job_id"])
    roles_by_companion: dict[str, list[str]] = {}
    candidates = [
        ("requester", row["requester"] or payload.get("requester") or result.get("requester")),
        ("executor", row["executor"] or payload.get("executor") or result.get("executor")),
        ("executor", row["companion"] or payload.get("companion") or contract.get("companion") or result.get("companion")),
    ]
    for role_name, raw_label in candidates:
        if not isinstance(raw_label, str):
            continue
        clean = raw_label.strip().lower()
        if not clean or clean in {"none", "unspecified", "null"}:
            continue
        bucket = roles_by_companion.setdefault(clean, [])
        if role_name not in bucket:
            bucket.append(role_name)

    specs: list[dict[str, Any]] = []
    for companion_id, roles in roles_by_companion.items():
        role_text = "/".join(roles)
        prefix = f"Participated in {task_key}"
        suffix = f" as {role_text}; outcome: {outcome_label}."
        if short_goal:
            budget = MAX_MEMORY_CHARS - len(prefix) - len(suffix) - 3
            goal_part = short_goal[: budget - 3].rstrip() + "..." if len(short_goal) > budget else short_goal
            content = f"{prefix} ({goal_part}){suffix}" if budget >= 12 else (prefix + suffix)[:MAX_MEMORY_CHARS]
        else:
            content = (prefix + suffix)[:MAX_MEMORY_CHARS]
        specs.append(
            {
                "profile_id": companion_id,
                "task_key": task_key,
                "kind": "note",
                "content": content,
                "tags": [
                    "task_participation",
                    f"task_participation:{task_key}",
                    f"task_outcome:{state}",
                ],
            }
        )
    return specs
