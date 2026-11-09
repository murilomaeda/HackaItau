"""Persistência em SQLite (arquivo único, WAL): snapshot de cada case, audit encadeado e sessões revogadas.

- `events` é append-only também no banco: triggers recusam UPDATE e DELETE. Quem editar o arquivo por fora quebra a
  cadeia HMAC (core/events.py), e `audit_heads` assina o tamanho da cadeia para que truncar o fim também apareça.
- O snapshot é JSON (nunca pickle): ler um banco adulterado não executa código.
"""

import json
import sqlite3
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from app.core.events import ChainCheck, head_signature, verify_chain
from app.core.schemas.agent import AgentResult
from app.core.schemas.events import Event
from app.core.schemas.evidence import EvidenceItem
from app.core.schemas.outputs import ReviewOutput

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    case_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    body TEXT NOT NULL,
    mac TEXT NOT NULL,
    PRIMARY KEY (case_id, seq)
);
CREATE TABLE IF NOT EXISTS audit_heads (
    case_id TEXT PRIMARY KEY,
    count INTEGER NOT NULL,
    signature TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revoked_sessions (
    jti TEXT PRIMARY KEY,
    expires_at INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
"""

_EVIDENCE = TypeAdapter(EvidenceItem)


class StateDB:
    def __init__(self, path: Path | None, audit_key: bytes) -> None:
        """path=None → banco em memória (testes)."""
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self._key = audit_key
        self._conn = sqlite3.connect(str(path) if path else ":memory:", check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            if path is not None:
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
        if path is not None:
            path.chmod(0o600)

    # ---- audit -------------------------------------------------------------------------------------------------
    def append_event(self, ev: Event, body: str, mac: str) -> None:
        signature = head_signature(self._key, ev.case_id, ev.seq, mac)
        with self._lock:
            self._conn.execute("BEGIN")
            self._conn.execute(
                "INSERT INTO events (case_id, seq, body, mac) VALUES (?, ?, ?, ?)", (ev.case_id, ev.seq, body, mac)
            )
            self._conn.execute(
                "INSERT INTO audit_heads (case_id, count, signature) VALUES (?, ?, ?) "
                "ON CONFLICT(case_id) DO UPDATE SET count = excluded.count, signature = excluded.signature",
                (ev.case_id, ev.seq, signature),
            )
            self._conn.execute("COMMIT")

    def _rows(self, case_id: str) -> list[tuple[str, str]]:
        with self._lock:
            return self._conn.execute("SELECT body, mac FROM events WHERE case_id = ? ORDER BY seq", (case_id,)).fetchall()

    def events(self, case_id: str) -> list[tuple[Event, str, str]]:
        return [(Event.model_validate_json(body), body, mac) for body, mac in self._rows(case_id)]

    def verify(self, case_id: str) -> ChainCheck:
        with self._lock:
            head = self._conn.execute("SELECT count, signature FROM audit_heads WHERE case_id = ?", (case_id,)).fetchone()
        return verify_chain(self._key, case_id, self._rows(case_id), tuple(head) if head else (0, ""))

    # ---- cases -------------------------------------------------------------------------------------------------
    def save_case(self, case_id: str, user_id: str, snapshot: dict[str, Any]) -> None:
        data = json.dumps(snapshot, ensure_ascii=False, default=str)
        with self._lock:
            self._conn.execute(
                "INSERT INTO cases (case_id, user_id, snapshot, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(case_id) DO UPDATE SET snapshot = excluded.snapshot, updated_at = excluded.updated_at",
                (case_id, user_id, data, time.time()),
            )

    def load_cases(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT snapshot FROM cases ORDER BY updated_at").fetchall()
        return [json.loads(r[0]) for r in rows]

    # ---- sessões -----------------------------------------------------------------------------------------------
    def revoke(self, jti: str, expires_at: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM revoked_sessions WHERE expires_at < ?", (int(time.time()),))
            self._conn.execute("INSERT OR IGNORE INTO revoked_sessions (jti, expires_at) VALUES (?, ?)", (jti, expires_at))

    def is_revoked(self, jti: str) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM revoked_sessions WHERE jti = ?", (jti,)).fetchone() is not None


# ---- snapshot de CaseRecord ------------------------------------------------------------------------------------
def dump_record(rec: Any) -> dict[str, Any]:
    """Tudo que um retry ou ajuste precisa para continuar depois de um reinício. A task asyncio não entra."""
    results = lambda d: {k: v.model_dump(mode="json") for k, v in d.items()}  # noqa: E731
    return {
        "state": rec.state.model_dump(mode="json"),
        "evidence": [item.model_dump(mode="json") for item in rec.evidence.items()],
        "answers": rec.answers,
        "job": asdict(rec.job) if rec.job else None,
        "completed": results(rec.completed),
        "reviews": {str(k): v.model_dump(mode="json") for k, v in rec.reviews.items()},
        "results": results(rec.results),
        "first_round": results(rec.first_round),
        "sticky_params": rec.sticky_params,
        "human_adjustments": rec.human_adjustments,
        "attachments": rec.attachments,
    }


def load_record_parts(snapshot: dict[str, Any]) -> dict[str, Any]:
    results = lambda d: {k: AgentResult.model_validate(v) for k, v in d.items()}  # noqa: E731
    return {
        "evidence": [_EVIDENCE.validate_python(i) for i in snapshot.get("evidence", [])],
        "answers": snapshot.get("answers", {}),
        "job": snapshot.get("job"),
        "completed": results(snapshot.get("completed", {})),
        "reviews": {int(k): ReviewOutput.model_validate(v) for k, v in snapshot.get("reviews", {}).items()},
        "results": results(snapshot.get("results", {})),
        "first_round": results(snapshot.get("first_round", {})),
        "sticky_params": snapshot.get("sticky_params", {}),
        "human_adjustments": snapshot.get("human_adjustments", 0),
        "attachments": snapshot.get("attachments", []),
    }
