"""Métricas de execução não equivalem a acurácia: correções, reworks e retries têm grãos distintos."""

import pytest

from app.agents.registry import AgentRegistry
from app.core.schemas.case import DemoOptions
from app.core.schemas.events import EventType as E
from app.core.store import CaseStore
from app.orchestration.metrics import compute_metrics

RISK = "agro_credit_risk"


@pytest.fixture
def recorded_case():
    store = CaseStore()
    rec = store.create("analyst-001", "demanda", DemoOptions(), "unconfigured")
    registry = AgentRegistry()

    def emit(kind, *, task="risk-R1", **payload):
        rec.events.emit(kind, payload, agent_id=RISK, task_id=task)

    def metrics():
        return next(a for a in compute_metrics(store, registry)["agents"] if a["agent_id"] == RISK)

    return emit, metrics, store, registry


def test_corrected_and_reopened_execution_is_not_subtracted_twice(recorded_case):
    emit, metrics, *_ = recorded_case
    emit(E.AGENT_STARTED)
    emit(E.GROUNDING_REJECTED)
    emit(E.OUTPUT_REJECTED)
    emit(E.AGENT_COMPLETED)
    emit(E.TASK_REOPENED, task=None, action=RISK, round=2)
    emit(E.AGENT_STARTED, task="risk-R2")
    emit(E.AGENT_COMPLETED, task="risk-R2")

    m = metrics()
    assert m["completed"] == 2 and m["validator_fixes"] == 1
    assert m["reopened_by_review"] == 1
    assert m["hits"] == 1 and m["accuracy_pct"] == 50 and m["first_pass_pct"] == 50
    assert m["completion_pct"] == 100
    assert m["validation_clean"] == 1 and m["validation_first_pass_pct"] == 50


def test_failed_attempt_does_not_contaminate_retry_with_the_same_task_id(recorded_case):
    emit, metrics, *_ = recorded_case
    emit(E.AGENT_STARTED)
    emit(E.OUTPUT_REJECTED)
    emit(E.EXECUTION_FAILED, task=None)
    emit(E.AGENT_STARTED)  # retry preserva task_id; é outra tentativa
    emit(E.AGENT_COMPLETED)

    m = metrics()
    assert m["runs"] == 2 and m["failed"] == 1 and m["completed"] == 1
    assert m["validator_fixes"] == 0 and m["validation_clean"] == 1
    assert m["completion_pct"] == 50 and m["validation_first_pass_pct"] == 100


def test_schema_retry_is_a_correction_even_without_output_rejected_event(recorded_case):
    emit, metrics, *_ = recorded_case
    emit(E.AGENT_STARTED)
    emit(E.LLM_CALLED, ok=True, attempt=1)
    emit(E.LLM_CALLED, ok=True, attempt=2)
    emit(E.AGENT_COMPLETED)

    m = metrics()
    assert m["validator_fixes"] == 1 and m["validation_clean"] == 0
    assert m["completion_pct"] == 100 and m["validation_first_pass_pct"] == 0


def test_pending_attempt_and_human_adjustment_are_not_errors(recorded_case):
    emit, metrics, *_ = recorded_case
    emit(E.AGENT_STARTED)
    assert metrics()["completion_pct"] is None
    assert metrics()["validation_first_pass_pct"] is None
    emit(E.AGENT_COMPLETED)
    emit(E.TASK_REOPENED, task=None, action=RISK, source="human", round=2)
    emit(E.AGENT_STARTED, task="risk-R2")

    m = metrics()
    assert m["in_progress"] == 1 and m["completion_pct"] == 100
    assert m["adjusted_by_human"] == 1 and m["reopened_by_review"] == 0 and m["errors"] == 0
    # pedido de ajuste do analista não é erro, mas a entrega não foi aceita como veio
    assert m["hits"] == 0 and m["first_pass_pct"] == 0


def test_same_task_id_in_new_execution_has_separate_validation_history(recorded_case):
    emit, metrics, *_ = recorded_case
    emit(E.AGENT_STARTED)
    emit(E.GROUNDING_REJECTED)
    emit(E.AGENT_COMPLETED)
    emit(E.AGENT_STARTED)  # nova execução após uma pendência de informação
    emit(E.AGENT_COMPLETED)
    assert metrics()["validator_fixes"] == 1
    assert metrics()["validation_clean"] == 1


def test_total_context_can_exceed_hypothetical_generalist(recorded_case):
    emit, _, store, registry = recorded_case
    for i in range(10):
        emit(E.AGENT_STARTED, task=str(i))
        emit(E.LLM_CALLED, task=str(i), ok=True, usage={"prompt_chars": 16000, "tokens_in": 3900, "tokens_out": 500})
        emit(E.AGENT_COMPLETED, task=str(i))

    m = compute_metrics(store, registry)
    assert m["context"]["squad_calls"] == 10 and m["context"]["generalist_calls"] == 1
    assert m["context"]["squad_tokens"] == 40000
    assert m["context"]["saved_tokens"] < 0  # não ajustar o comparador para forçar uma economia
    assert m["tokens"] == {"input": 39000, "output": 5000}  # uso real separado da estimativa por caracteres


def test_first_pass_counts_failures_and_each_attempt_once(recorded_case):
    emit, metrics, *_ = recorded_case
    emit(E.AGENT_STARTED, task="a")
    emit(E.AGENT_COMPLETED, task="a")  # aceita de primeira
    emit(E.AGENT_STARTED, task="b")
    emit(E.EXECUTION_FAILED, task=None)  # falha
    emit(E.AGENT_STARTED, task="c")
    emit(E.OUTPUT_REJECTED, task="c")
    emit(E.AGENT_COMPLETED, task="c")
    emit(E.TASK_REOPENED, task=None, action=RISK, round=2)  # refeita no formato E devolvida: conta uma vez
    m = metrics()
    assert m["finished"] == 3 and m["hits"] == 1 and m["first_pass_pct"] == pytest.approx(33.3)


def test_reviewer_rate_ignores_the_last_round_without_a_follow_up_review():
    store = CaseStore()
    rec = store.create("analyst-001", "demanda", DemoOptions(), "unconfigured")
    review = "credit_review"

    def issue(code):
        rec.events.emit(E.REVIEW_ISSUE_FOUND, {"severity": "high", "code": code, "owner": RISK}, agent_id=review)

    rec.events.emit(E.REVIEW_STARTED, {}, agent_id=review)
    issue("CALC_INCONSISTENT")
    issue("ASSUMPTION_ABOVE_BASELINE_UNJUSTIFIED")
    rec.events.emit(E.REVIEW_COMPLETED, {"reexecution_required": True}, agent_id=review)
    rec.events.emit(E.REVIEW_STARTED, {}, agent_id=review)
    issue("CALC_INCONSISTENT")  # continuou depois do retrabalho
    issue("POLICY_THRESHOLD_BREACH")  # novo, sem rodada seguinte: fora da conta
    rec.events.emit(E.REVIEW_COMPLETED, {"reexecution_required": False}, agent_id=review)
    r = next(a for a in compute_metrics(store, AgentRegistry())["agents"] if a["agent_id"] == review)["reviewer"]
    assert r["material_findings"] == 4 and r["checked_after_rework"] == 2
    assert r["confirmed_by_rework"] == 1 and r["confirmation_pct"] == 50
