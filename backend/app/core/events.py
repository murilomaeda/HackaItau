"""EventLog append-only por case (ARCHITECTURE.md §15). Payloads pequenos: ids, códigos, contagens, flags.

Cada evento é encadeado ao anterior por HMAC-SHA256 (chave do servidor): alterar, remover ou reordenar um evento —
na memória ou no SQLite — quebra a cadeia a partir dali, e `verify` diz onde.
"""

import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from app.core.schemas.events import AUDIT_EVENT_TYPES, Event, EventType

GENESIS = "0" * 64
_DEFAULT_KEY = secrets.token_bytes(32)  # sem chave do servidor (testes de unidade): vale só neste processo


def canonical(ev: Event) -> str:
    return json.dumps(ev.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def link(key: bytes, prev_mac: str, body: str) -> str:
    return hmac.new(key, f"{prev_mac}\n{body}".encode(), hashlib.sha256).hexdigest()


def head_signature(key: bytes, case_id: str, count: int, last_mac: str) -> str:
    """Assina o tamanho da cadeia: apagar os últimos eventos também é detectado."""
    return link(key, last_mac, f"head:{case_id}:{count}")


class ChainCheck(BaseModel):
    ok: bool
    events: int
    broken_at_seq: int | None = None
    reason: str | None = None


def verify_chain(key: bytes, case_id: str, rows: list[tuple[str, str]], head: tuple[int, str] | None) -> ChainCheck:
    """rows = [(corpo canônico, mac)] na ordem gravada; head = (contagem, assinatura) quando houver."""
    prev = GENESIS
    for i, (body, mac) in enumerate(rows, start=1):
        try:
            seq = json.loads(body).get("seq")
        except ValueError:
            seq = None
        if seq != i:
            return ChainCheck(ok=False, events=len(rows), broken_at_seq=i, reason="sequence_gap")
        if not hmac.compare_digest(link(key, prev, body), mac):
            return ChainCheck(ok=False, events=len(rows), broken_at_seq=i, reason="mac_mismatch")
        prev = mac
    if head is not None:
        count, signature = head
        if count != len(rows) or not hmac.compare_digest(head_signature(key, case_id, count, prev), signature):
            return ChainCheck(ok=False, events=len(rows), broken_at_seq=min(count, len(rows)) + 1, reason="truncated")
    return ChainCheck(ok=True, events=len(rows))


EventSink = Callable[[Event, str, str], None]  # (evento, corpo canônico, mac)


class EventLog:
    def __init__(
        self,
        case_id: str,
        *,
        key: bytes | None = None,
        sink: EventSink | None = None,
        restored: list[tuple[Event, str, str]] | None = None,
    ) -> None:
        self.case_id = case_id
        self._key = key or _DEFAULT_KEY
        self._sink = sink
        # restored = [(evento, corpo canônico gravado, mac)]: verifica o que foi gravado, sem re-serializar
        self._events: list[Event] = [e for e, _, _ in restored or []]
        self._bodies: list[str] = [b for _, b, _ in restored or []]
        self._macs: list[str] = [m for _, _, m in restored or []]

    def emit(
        self,
        type_: EventType,
        payload: dict[str, Any] | None = None,
        *,
        agent_id: str | None = None,
        task_id: str | None = None,
    ) -> Event:
        ev = Event(
            seq=len(self._events) + 1,
            ts=datetime.now(timezone.utc),
            case_id=self.case_id,
            type=type_,
            agent_id=agent_id,
            task_id=task_id,
            payload=payload or {},
            audit=type_ in AUDIT_EVENT_TYPES,
        )
        body = canonical(ev)
        mac = link(self._key, self._macs[-1] if self._macs else GENESIS, body)
        self._events.append(ev)
        self._bodies.append(body)
        self._macs.append(mac)
        if self._sink is not None:
            self._sink(ev, body, mac)
        return ev

    def verify(self) -> ChainCheck:
        return verify_chain(self._key, self.case_id, list(zip(self._bodies, self._macs, strict=True)), None)

    def all(self) -> list[Event]:
        return list(self._events)

    def list_after(self, seq: int) -> list[Event]:
        return [e for e in self._events if e.seq > seq]

    def of_type(self, *types: EventType) -> list[Event]:
        wanted = set(types)
        return [e for e in self._events if e.type in wanted]

    @property
    def last_seq(self) -> int:
        return len(self._events)

    def __len__(self) -> int:
        return len(self._events)
