"""Cenários de stress — funções puras. Choques fixos vêm de policies.json; cada cenário re-executa credit_metrics."""

from pydantic import BaseModel

from app.calculations.credit_metrics import CoverageClass, MetricInputs, credit_metrics
from app.calculations.policy_params import StressScenario, Thresholds

SHOCKABLE = ("price", "productivity", "cost_per_hectare")


class ScenarioOutcome(BaseModel):
    scenario_id: str
    label: str
    shocks: dict[str, float]
    inputs: MetricInputs
    coverage: float
    expected_cash_generation: float
    pro_forma_leverage: float
    classification: CoverageClass


def apply_shocks(base: MetricInputs, shocks: dict[str, float]) -> MetricInputs:
    values = base.model_dump()
    for name, pct in shocks.items():
        if name not in SHOCKABLE:
            raise ValueError(f"choque não permitido: {name}")
        values[name] = round(values[name] * (1 + pct), 6)
    return MetricInputs(**values)


def run_scenarios(base: MetricInputs, scenarios: list[StressScenario], t: Thresholds) -> list[ScenarioOutcome]:
    out = []
    for sc in scenarios:
        shocked = apply_shocks(base, sc.shocks)
        m = credit_metrics(shocked, t)
        out.append(
            ScenarioOutcome(
                scenario_id=sc.scenario_id,
                label=sc.label,
                shocks=sc.shocks,
                inputs=shocked,
                coverage=m.coverage,
                expected_cash_generation=m.expected_cash_generation,
                pro_forma_leverage=m.pro_forma_leverage,
                classification=m.coverage_class,
            )
        )
    return out


def worst_class(outcomes: list[ScenarioOutcome]) -> CoverageClass:
    order: list[CoverageClass] = ["comfortable", "reduced_buffer", "attention_required", "insufficient"]
    return max((o.classification for o in outcomes), key=order.index, default="comfortable")


class BreakEven(BaseModel):
    """Quanto cada premissa aguenta de choque isolado até a cobertura cair a `target_coverage`.
    Valor negativo: a cobertura já está abaixo do alvo no cenário base."""

    target_coverage: float
    max_price_drop_pct: float
    max_productivity_drop_pct: float
    max_cost_increase_pct: float


def break_even(base: MetricInputs, target_coverage: float) -> BreakEven:
    """Fechado na fórmula: caixa = área·prod·preço − área·custo; cobertura = caixa / valor pedido."""
    a, prod, price, cost, req = (
        base.planted_area_hectares,
        base.productivity,
        base.price,
        base.cost_per_hectare,
        base.requested_amount,
    )
    needed_revenue = target_coverage * req + a * cost  # receita mínima para atingir o alvo
    revenue = a * prod * price
    drop = 1 - needed_revenue / revenue if revenue > 0 else -1.0
    max_cost = (revenue - target_coverage * req) / a if a > 0 else 0.0
    cost_up = max_cost / cost - 1 if cost > 0 else 0.0
    return BreakEven(
        target_coverage=target_coverage,
        max_price_drop_pct=round(drop * 100, 1),
        max_productivity_drop_pct=round(drop * 100, 1),  # receita é linear em preço e em produtividade
        max_cost_increase_pct=round(cost_up * 100, 1),
    )
