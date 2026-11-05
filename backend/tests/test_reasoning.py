"""Raciocínio do Risk: tese de pagamento, ponto de quebra calculado por código e perguntas de diligência."""

import pytest

from app.calculations.credit_metrics import MetricInputs, credit_metrics
from app.calculations.policy_params import load_policy_params
from app.calculations.stress import apply_shocks, break_even
from tests.test_s3_orchestrator import _bootstrap, _container

BASE = MetricInputs(
    planted_area_hectares=10_000,
    productivity=60,
    price=120,
    cost_per_hectare=4_000,
    requested_amount=20_000_000,
    net_debt=100_000_000,
    ebitda=60_000_000,
)


@pytest.mark.parametrize("target", [1.0, 1.5])
def test_break_even_shock_lands_exactly_on_target_coverage(target):
    t = load_policy_params().thresholds
    be = break_even(BASE, target)
    for field, pct in (
        ("price", -be.max_price_drop_pct),
        ("productivity", -be.max_productivity_drop_pct),
        ("cost_per_hectare", be.max_cost_increase_pct),
    ):
        shocked = apply_shocks(BASE, {field: pct / 100})
        assert credit_metrics(shocked, t).coverage == pytest.approx(target, abs=0.01), field


def test_break_even_is_negative_when_base_is_already_below_target():
    weak = BASE.model_copy(update={"requested_amount": 200_000_000})
    assert credit_metrics(weak, load_policy_params().thresholds).coverage < 1
    assert break_even(weak, 1.0).max_price_drop_pct < 0


async def test_report_carries_the_argument_not_just_fields():
    container = _container()
    rec = await _bootstrap(container)
    await container.orchestrator.run(rec.state.case_id)
    a = rec.state.report.analysis
    assert a.repayment_thesis and a.questions_for_analyst and a.critical_assumptions
    assert a.break_even_calculation_id and a.break_even_calculation_id.startswith("CALC-")
    assert [b.target_coverage for b in a.break_even] == [1.0, 1.5]
    stress = next(c for c in rec.state.report.calculations if c.calculation_id == a.break_even_calculation_id)
    assert stress.outputs["break_even"][0]["max_price_drop_pct"] == a.break_even[0].max_price_drop_pct
