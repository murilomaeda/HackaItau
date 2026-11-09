"""Credit Review — camada A: validators determinísticos (ARCHITECTURE.md §6.4-A). Sem LLM.

Cada função recebe o mesmo contexto e devolve findings; regras são gerais (não codificam "produtividade 61").
"""

import re
from dataclasses import dataclass, field
from typing import Any

from app.agents.review.remediations import (
    ASSUMPTION_ABOVE_BASELINE_UNJUSTIFIED,
    RISK_IGNORED_BY_STRUCTURE,
    required_action,
)
from app.agents.runtime import collect_ids
from app.calculations.credit_metrics import MetricInputs, credit_metrics
from app.calculations.policy_params import PolicyParams
from app.core.crops import product_supports_crop
from app.core.events import EventLog
from app.core.evidence import EvidenceRegistry
from app.core.schemas.agent import AgentResult
from app.core.schemas.events import EventType
from app.core.schemas.evidence import CalculationRecord, SourceRecord
from app.core.schemas.outputs import EligibilityOutput, Finding, RiskOutput, Severity, StructuringOutput
from app.governance.output_guard import LANGUAGE_RE, NEGATION_RE
from app.tools.calc_tools import CALC_CREDIT_METRICS

ELIGIBILITY, RISK, STRUCTURING = "agro_eligibility", "agro_credit_risk", "agro_structuring"
EPS = 1e-6
HISTORICAL_PREFIX = "historical_"


@dataclass
class ReviewContext:
    results: dict[str, AgentResult]  # último round de cada agente
    evidence: EvidenceRegistry
    events: EventLog
    policy: PolicyParams
    requested_amount: float | None
    requested_crop: str | None = None
    _seq: int = field(default=0, init=False)

    def finding(
        self,
        code: str,
        severity: Severity,
        message: str,
        owner: str | None,
        evidence_ids: list[str] | None = None,
        status: str = "open",
    ) -> Finding:
        self._seq += 1
        return Finding(
            id=f"F-{self._seq:03d}",
            code=code,
            severity=severity,
            message=message,
            owner_agent=owner,
            evidence_ids=list(dict.fromkeys(evidence_ids or [])),
            origin="validator",
            required_action=required_action(code),
            status=status,  # type: ignore[arg-type]
        )

    def output(self, agent_id: str) -> dict[str, Any] | None:
        r = self.results.get(agent_id)
        return r.output if r else None

    def calc(self, calc_id: str) -> CalculationRecord | None:
        item = self.evidence.get(calc_id)
        return item if isinstance(item, CalculationRecord) else None


# ------------------------------------------------------------------ validators


def evidence_not_found(ctx: ReviewContext) -> list[Finding]:
    out = []
    for agent_id, r in ctx.results.items():
        missing = ctx.evidence.missing(collect_ids(r.output))
        if missing:
            out.append(
                ctx.finding(
                    "EVIDENCE_NOT_FOUND",
                    "high",
                    f"Cita fontes que não existem neste caso: {', '.join(sorted(set(missing)))}.",
                    agent_id,
                )
            )
    return out


def calc_inconsistent(ctx: ReviewContext) -> list[Finding]:
    risk = ctx.output(RISK)
    if not risk:
        return []
    out = []
    ro = RiskOutput.model_validate(risk)
    calc = ctx.calc(ro.metrics.calculation_id)
    if calc is None or calc.name != CALC_CREDIT_METRICS:
        return [
            ctx.finding(
                "CALC_INCONSISTENT",
                "high",
                "Os indicadores de risco não apontam para o cálculo de capacidade de pagamento.",
                RISK,
            )
        ]
    inputs = MetricInputs.model_validate({k: v for k, v in calc.inputs.items() if k in MetricInputs.model_fields})
    recomputed = credit_metrics(inputs, ctx.policy.thresholds).model_dump()
    diffs = [
        k
        for k in (
            "expected_revenue",
            "crop_cost",
            "expected_cash_generation",
            "net_debt_ebitda",
            "pro_forma_leverage",
            "coverage",
        )
        if abs(float(recomputed[k]) - float(calc.outputs[k])) > EPS
        or abs(float(recomputed[k]) - getattr(ro.metrics, k)) > EPS
    ]
    if diffs:
        out.append(
            ctx.finding(
                "CALC_INCONSISTENT",
                "high",
                f"Refazendo as contas, estes valores não batem com os informados: {', '.join(_label(k) for k in diffs)}.",
                RISK,
                [calc.id],
            )
        )
    return out


def assumption_above_baseline_unjustified(ctx: ReviewContext) -> list[Finding]:
    r = ctx.results.get(RISK)
    if not r:
        return []
    out = []
    for a in r.assumptions:
        if a.origin != "code" or a.source_id is None or not isinstance(a.value, (int, float)):
            continue
        src = ctx.evidence.get(a.source_id)
        if not isinstance(src, SourceRecord):
            continue
        hist = src.data.get(HISTORICAL_PREFIX + a.name)
        if not isinstance(hist, (int, float)) or float(a.value) <= float(hist):
            continue
        if a.justification.strip():
            continue
        out.append(
            ctx.finding(
                ASSUMPTION_ABOVE_BASELINE_UNJUSTIFIED,
                "high",
                f"{_cap(_label(a.name))} usada ({_n(a.value)}) está acima da média histórica "
                f"do próprio cliente ({_n(hist)}), sem explicação para isso.",
                RISK,
                [a.source_id, *r.calculation_ids],
            )
        )
    return out


def policy_threshold_breach(ctx: ReviewContext) -> list[Finding]:
    risk = ctx.output(RISK)
    if not risk:
        return []
    ro = RiskOutput.model_validate(risk)
    t = ctx.policy.thresholds
    risk_text = " ".join(f"{i.code} {i.message}" for i in ro.main_risks).lower()
    breaches = []
    if ro.metrics.pro_forma_leverage > t.pro_forma_leverage_max + EPS:
        breaches.append(("pro_forma_leverage", ro.metrics.pro_forma_leverage, t.pro_forma_leverage_max, "leverage|alavanc"))
    if ro.metrics.net_debt_ebitda > t.net_debt_ebitda_max + EPS:
        # A métrica também é descrita como alavancagem atual/líquida. Reconhecer só
        # "dívida" ou "EBITDA" produzia falso positivo para essas formulações usuais.
        current_leverage = r"ebitda|d[ií]vida|alavancagem\s+(?:atual|l[ií]quida)|(?:net|current)[_ -]leverage"
        breaches.append(("net_debt_ebitda", ro.metrics.net_debt_ebitda, t.net_debt_ebitda_max, current_leverage))
    out = []
    for name, value, limit, pattern in breaches:
        if re.search(pattern, risk_text):
            continue  # já reconhecido pelo Risk
        out.append(
            ctx.finding(
                "POLICY_THRESHOLD_BREACH",
                "medium",
                f"{_cap(_label(name))} de {_n(value)} passa do limite da política ({_n(limit)}) "
                "e não aparece entre os riscos.",
                RISK,
                [ro.metrics.calculation_id, *t.kb_ids()],
            )
        )
    return out


def mandatory_field_missing(ctx: ReviewContext) -> list[Finding]:
    out = []
    elig = ctx.output(ELIGIBILITY)
    if elig:
        eo = EligibilityOutput.model_validate(elig)
        for m in eo.missing_items:
            if m.blocking:
                out.append(
                    ctx.finding("MANDATORY_FIELD_MISSING", "high", f"Falta um item obrigatório: {m.item}.", ELIGIBILITY)
                )
    struct = ctx.output(STRUCTURING)
    if struct:
        so = StructuringOutput.model_validate(struct)
        for alt in so.alternatives:
            empty = [f for f in ("guarantees", "conditions", "rationale") if not getattr(alt, f)]
            if empty:
                out.append(
                    ctx.finding(
                        "MANDATORY_FIELD_MISSING",
                        "medium",
                        f"{alt.id}: sem {', '.join(_label(f) for f in empty)}.",
                        STRUCTURING,
                        alt.evidence_ids,
                    )
                )
    return out


def structure_vs_request_and_catalog(ctx: ReviewContext) -> list[Finding]:
    struct = ctx.output(STRUCTURING)
    if not struct:
        return []
    so = StructuringOutput.model_validate(struct)
    catalog = {str(s.data.get("product_id")): s for s in ctx.evidence.sources() if s.resource_domain == "product_catalog"}
    out = []
    for alt in so.alternatives:
        if ctx.requested_amount is not None and alt.amount > ctx.requested_amount + EPS:
            out.append(
                ctx.finding(
                    "STRUCTURE_EXCEEDS_REQUEST",
                    "medium",
                    f"{alt.id}: valor de {_brl(alt.amount)} maior que o pedido ({_brl(ctx.requested_amount)}).",
                    STRUCTURING,
                    alt.evidence_ids,
                )
            )
        prod = catalog.get(alt.product_id)
        if prod is None:
            out.append(
                ctx.finding(
                    "PRODUCT_UNKNOWN", "high", f"{alt.id}: o produto {alt.product_id} não está no catálogo.", STRUCTURING
                )
            )
            continue
        if ctx.requested_crop is not None and not product_supports_crop(prod.data, ctx.requested_crop):
            out.append(
                ctx.finding(
                    "PRODUCT_CROP_MISMATCH",
                    "high",
                    f"{alt.id}: o produto {alt.product_id} não financia {ctx.requested_crop}.",
                    STRUCTURING,
                    [prod.id, *alt.evidence_ids],
                )
            )
        lo, hi = prod.data.get("tenor_months_min"), prod.data.get("tenor_months_max")
        if lo is not None and hi is not None and not int(lo) <= alt.tenor_months <= int(hi):
            out.append(
                ctx.finding(
                    "TENOR_OUT_OF_RANGE",
                    "medium",
                    f"{alt.id}: prazo de {alt.tenor_months} meses fora do que o produto permite ({lo} a {hi} meses).",
                    STRUCTURING,
                    [prod.id, *alt.evidence_ids],
                )
            )
    return out


def risk_ignored_by_structure(ctx: ReviewContext) -> list[Finding]:
    risk, struct = ctx.output(RISK), ctx.output(STRUCTURING)
    if not risk or not struct:
        return []
    ro, so = RiskOutput.model_validate(risk), StructuringOutput.model_validate(struct)
    addressed: set[str] = set()
    for alt in so.alternatives:
        addressed |= {c.upper() for c in alt.addressed_risk_codes}
    ignored = [r for r in ro.main_risks if r.severity in ("high", "medium") and r.code.upper() not in addressed]
    if not ignored:
        return []
    worst: Severity = "high" if any(r.severity == "high" for r in ignored) else "medium"
    codes = "; ".join(r.message.rstrip(".") for r in ignored)
    ev = [i for r in ignored for i in r.evidence_ids] + [ctx.results[RISK].output_id]
    return [
        ctx.finding(
            RISK_IGNORED_BY_STRUCTURE,
            worst,
            f"Nenhuma das alternativas trata estes riscos apontados na análise de risco: {codes}.",
            STRUCTURING,
            ev,
        )
    ]


def permission_violation_attempted(ctx: ReviewContext) -> list[Finding]:
    by_agent: dict[str, list[int]] = {}
    for e in ctx.events.of_type(EventType.PERMISSION_DENIED):
        by_agent.setdefault(e.agent_id or "unknown", []).append(e.seq)
    return [
        ctx.finding(
            "PERMISSION_VIOLATION_ATTEMPTED",
            "info",
            f"{len(seqs)} {'pedido de acesso bloqueado' if len(seqs) == 1 else 'pedidos de acesso bloqueados'} "
            "pelas permissões; nada foi liberado.",
            agent_id,
            status="informational",
        )
        for agent_id, seqs in sorted(by_agent.items())
    ]


def approval_language(ctx: ReviewContext) -> list[Finding]:
    out = []
    for agent_id, r in ctx.results.items():
        hits = []
        for text in _strings(r.output):
            for m in LANGUAGE_RE.finditer(text):
                if not NEGATION_RE.search(text[: m.start()]):
                    hits.append(m.group(0))
        if hits:
            out.append(
                ctx.finding(
                    "APPROVAL_LANGUAGE",
                    "medium",
                    f"O texto fala como se a decisão já estivesse tomada: {', '.join(sorted(set(hits))[:5])}.",
                    agent_id,
                    [r.output_id],
                )
            )
    return out


_LABELS = {
    "expected_revenue": "receita esperada",
    "crop_cost": "custo da lavoura",
    "expected_cash_generation": "geração de caixa",
    "net_debt_ebitda": "dívida líquida sobre EBITDA",
    "pro_forma_leverage": "alavancagem depois da operação",
    "coverage": "cobertura",
    "productivity": "produtividade",
    "price": "preço",
    "planted_area": "área plantada",
    "cost_per_hectare": "custo por hectare",
    "guarantees": "garantias",
    "conditions": "condições",
    "rationale": "justificativa",
}


def _label(key: str) -> str:
    return _LABELS.get(key, key.replace("_", " "))


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]  # str.capitalize() baixaria o resto ("EBITDA" → "ebitda")


def _n(value: float) -> str:
    return f"{float(value):,.2f}".rstrip("0").rstrip(".").replace(",", "X").replace(".", ",").replace("X", ".")


def _brl(value: float | None) -> str:
    return "R$ " + f"{float(value or 0):,.0f}".replace(",", ".")


VALIDATORS = (
    evidence_not_found,
    calc_inconsistent,
    assumption_above_baseline_unjustified,
    policy_threshold_breach,
    mandatory_field_missing,
    structure_vs_request_and_catalog,
    risk_ignored_by_structure,
    permission_violation_attempted,
    approval_language,
)


def run_validators(ctx: ReviewContext) -> list[Finding]:
    findings: list[Finding] = []
    for v in VALIDATORS:
        findings.extend(v(ctx))
    return findings


def _strings(obj: Any) -> list[str]:
    acc: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            acc.append(node)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for i in node:
                walk(i)

    walk(obj)
    return acc
