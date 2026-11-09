"""CaseStore: um CaseRecord agrupa estado, eventos e evidências.

Com `StateDB`, cada evento é gravado na hora (cadeia HMAC) e o snapshot do case a cada `touch()`; ao subir, os cases
voltam do banco. Uma execução que estava rodando quando o processo caiu volta como `failed` e pode ser retomada.
Sem `StateDB`, tudo fica só em memória, como antes.
"""

import asyncio
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.core.events import EventLog
from app.core.evidence import EvidenceRegistry
from app.core.persistence import StateDB, dump_record, load_record_parts
from app.core.schemas.agent import AgentResult
from app.core.schemas.case import AgentStatus, CaseState, CaseStatus, DemoOptions
from app.core.schemas.events import EventType
from app.core.schemas.outputs import ReviewOutput

# estados em que havia trabalho em andamento: depois de um reinício não há task rodando, então viram falha retomável
_INTERRUPTED = {CaseStatus.running, CaseStatus.reviewing, CaseStatus.consolidating, CaseStatus.interpreting}


@dataclass
class RunJob:
    """O que o Orchestrator está executando — é o que um retry retoma."""

    kind: str  # "execution" | "human_adjustment"
    round: int = 1
    target_agent: str | None = None
    comment: str = ""  # comentário do analista (UNTRUSTED no prompt)


@dataclass
class CaseRecord:
    state: CaseState
    events: EventLog
    evidence: EvidenceRegistry = field(default_factory=EvidenceRegistry)
    answers: dict[str, Any] = field(default_factory=dict)  # respostas do usuário após scope congelado (UNTRUSTED)
    run_task: asyncio.Task[None] | None = field(default=None, repr=False)
    # checkpoints da execução: um retry reaproveita o que já concluiu em vez de repetir chamadas ao LLM
    job: RunJob | None = None
    completed: dict[str, AgentResult] = field(default_factory=dict)  # "agent_id@R<n>" → resultado
    reviews: dict[int, ReviewOutput] = field(default_factory=dict)  # rodada → review consolidado
    results: dict[str, AgentResult] = field(default_factory=dict)  # último resultado de cada agente (base de ajustes)
    first_round: dict[str, AgentResult] = field(default_factory=dict)
    # params de rework que continuam valendo nas rodadas seguintes (ex.: baseline histórico do Risk)
    sticky_params: dict[str, dict[str, Any]] = field(default_factory=dict)
    human_adjustments: int = 0
    attachments: list[dict[str, Any]] = field(default_factory=list)  # documentos anexados (registros como documents.json)
    persist: Callable[["CaseRecord"], None] | None = field(default=None, repr=False)

    def touch(self) -> None:
        self.state.updated_at = datetime.now(timezone.utc)
        self.state.last_event_seq = self.events.last_seq
        if self.persist is not None:
            self.persist(self)


class CaseStore:
    def __init__(self, db: StateDB | None = None, audit_key: bytes | None = None) -> None:
        self._cases: dict[str, CaseRecord] = {}
        self._db = db
        self._key = audit_key
        if db is not None:
            for snapshot in db.load_cases():
                self._restore(snapshot)

    def _save(self, rec: CaseRecord) -> None:
        assert self._db is not None
        self._db.save_case(rec.state.case_id, rec.state.user_id, dump_record(rec))

    def _events(self, case_id: str, restored: list | None = None) -> EventLog:
        sink = self._db.append_event if self._db is not None else None
        return EventLog(case_id, key=self._key, sink=sink, restored=restored)

    def _restore(self, snapshot: dict[str, Any]) -> None:
        assert self._db is not None
        state = CaseState.model_validate(snapshot["state"])
        parts = load_record_parts(snapshot)
        evidence = EvidenceRegistry()
        for item in parts.pop("evidence"):
            evidence.register(item)
        job = parts.pop("job")
        rec = CaseRecord(
            state=state,
            events=self._events(state.case_id, self._db.events(state.case_id)),
            evidence=evidence,
            job=RunJob(**job) if job else None,
            persist=self._save,
            **parts,
        )
        if state.status in _INTERRUPTED:
            state.status = CaseStatus.failed
            state.error = "interrupted:server_restart"
            for agent in state.agents:
                if agent.status == AgentStatus.running:
                    agent.status = AgentStatus.failed
            rec.events.emit(EventType.EXECUTION_FAILED, {"error": state.error})
            rec.touch()
        self._cases[state.case_id] = rec

    def create(self, user_id: str, prompt: str, demo_options: DemoOptions, llm_mode: str) -> CaseRecord:
        case_id = f"case-{secrets.token_hex(8)}"
        now = datetime.now(timezone.utc)
        state = CaseState(
            case_id=case_id,
            status=CaseStatus.created,
            user_id=user_id,
            prompt=prompt,
            demo_options=demo_options,
            created_at=now,
            updated_at=now,
            llm_mode=llm_mode,
        )
        record = CaseRecord(state=state, events=self._events(case_id), persist=self._save if self._db is not None else None)
        self._cases[case_id] = record
        record.touch()
        return record

    def verify_audit(self, case_id: str):
        """Cadeia do banco quando há persistência (é o que sobrevive); senão, a da memória."""
        if self._db is not None:
            return self._db.verify(case_id)
        return self._cases[case_id].events.verify()

    def get(self, case_id: str) -> CaseRecord | None:
        return self._cases.get(case_id)

    def all(self) -> list[CaseRecord]:
        return list(self._cases.values())

    def __contains__(self, case_id: object) -> bool:
        return case_id in self._cases

    def __len__(self) -> int:
        return len(self._cases)
