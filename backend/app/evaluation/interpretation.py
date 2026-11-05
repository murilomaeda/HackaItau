"""Benchmark de leitura do pedido: o mesmo tipo de pedido escrito de 36 jeitos diferentes.

    python -m app.evaluation.interpretation                    # só a heurística (offline, sem chave)
    python -m app.evaluation.interpretation --llm              # heurística × leitura com LLM (usa LLM_API_KEY)
    python -m app.evaluation.interpretation --llm --repetitions 3

O benchmark original da squad começa de uma "demanda estruturada" e por isso nunca mediu se a Atena entende
pedidos escritos de outro jeito. Este mede exatamente isso, campo a campo, contra um gabarito.

Pontuação por campo (cliente, valor, finalidade, cultura, safra):
- gabarito com valor → acerto se a leitura for igual (cliente comparado sem sufixo societário);
- gabarito null → acerto se a leitura também for null (inventar dado é erro);
- gabarito "?" (ambíguo) → acerto se a leitura for null ou se houver pergunta de confirmação.
"""

import argparse
import asyncio
import json
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from app.config import REPO_ROOT, Settings
from app.core.schemas.outputs import InterpretedDemand
from app.llm.openai_compat import OpenAICompatProvider
from app.orchestration.interpreter import _client_core, heuristic_interpret, interpret

CASES_PATH = Path(__file__).with_name("interpretation_cases.json")
FIELDS = ("client", "amount", "purpose", "crop", "cycle")
_ATTR = {"client": "client_ref", "amount": "requested_amount", "purpose": "purpose", "crop": "crop", "cycle": "cycle"}


def load_cases() -> dict[str, Any]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def _value(it: InterpretedDemand, field: str) -> Any:
    v = getattr(it, _ATTR[field])
    if field == "client" and v is not None:
        return _client_core(str(v))
    return v


def score_field(it: InterpretedDemand, field: str, gold: Any) -> bool:
    got = _value(it, field)
    if gold == "?":
        return got is None or bool(it.ambiguities)
    if gold is None:
        return got is None
    if field == "amount":
        return got is not None and abs(float(got) - float(gold)) < 0.5
    return got == gold


def score_case(it: InterpretedDemand, case: dict[str, Any]) -> dict[str, bool]:
    return {f: score_field(it, f, case["gold"][f]) for f in FIELDS}


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    per_field = {f: sum(r["score"][f] for r in rows) for f in FIELDS}
    full = sum(all(r["score"].values()) for r in rows)
    return {
        "cases": n,
        "fully_correct": full,
        "fully_correct_pct": round(100 * full / n, 1) if n else 0.0,
        "field_accuracy_pct": {f: round(100 * c / n, 1) for f, c in per_field.items()},
        "overall_field_accuracy_pct": round(100 * sum(per_field.values()) / (n * len(FIELDS)), 1) if n else 0.0,
    }


class _CountingProvider:
    def __init__(self, inner: OpenAICompatProvider) -> None:
        self.inner, self.tokens_in, self.tokens_out = inner, 0, 0

    async def complete(self, **kw: Any):
        resp = await self.inner.complete(**kw)
        self.tokens_in += resp.usage.tokens_in
        self.tokens_out += resp.usage.tokens_out
        return resp


def _row(case: dict[str, Any], it: InterpretedDemand, seconds: float, rep: int) -> dict[str, Any]:
    return {
        "id": case["id"],
        "repetition": rep,
        "score": score_case(it, case),
        "read": {f: _value(it, f) for f in FIELDS},
        "ambiguities": it.ambiguities,
        "notes": it.notes,
        "seconds": round(seconds, 2),
    }


async def run(llm: bool, repetitions: int, model: str | None) -> dict[str, Any]:
    data = load_cases()
    today = date.fromisoformat(data["today"])
    out: dict[str, Any] = {"version": data["version"], "generated_at": datetime.now(timezone.utc).isoformat()}

    heur = [_row(c, heuristic_interpret(c["text"]), 0.0, 1) for c in data["cases"]]
    out["heuristic"] = {"summary": summarize(heur), "rows": heur}

    if llm:
        s = Settings()
        provider = _CountingProvider(
            OpenAICompatProvider(
                base_url=s.llm_base_url,
                api_key=s.llm_api_key,
                timeout_seconds=s.llm_timeout_seconds,
                secret_values=s.secret_values(),
                max_retries=s.llm_max_retries,
            )
        )
        model = model or s.llm_model
        rows = []
        for rep in range(1, repetitions + 1):
            for c in data["cases"]:
                t0 = time.perf_counter()
                it = await interpret(c["text"], provider, model, today=today)
                rows.append(_row(c, it, time.perf_counter() - t0, rep))
        out["llm"] = {
            "model": model,
            "repetitions": repetitions,
            "tokens_in": provider.tokens_in,
            "tokens_out": provider.tokens_out,
            "summary": summarize(rows),
            "rows": rows,
        }
    return out


def _print(result: dict[str, Any]) -> None:
    modes = [m for m in ("heuristic", "llm") if m in result]
    print(f"{'':28}" + "".join(f"{m:>14}" for m in modes))
    for f in FIELDS:
        print(f"{f:28}" + "".join(f"{result[m]['summary']['field_accuracy_pct'][f]:>13}%" for m in modes))
    print(f"{'todos os campos certos':28}" + "".join(f"{result[m]['summary']['fully_correct_pct']:>13}%" for m in modes))
    for m in modes:
        wrong = [r["id"] for r in result[m]["rows"] if not all(r["score"].values())]
        print(f"\n{m}: {len(wrong)} casos com erro: {', '.join(sorted(set(wrong))) or '-'}")
    if "llm" in result:
        print(
            f"\ntokens: {result['llm']['tokens_in']} entrada, {result['llm']['tokens_out']} saída ({result['llm']['model']})"
        )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--llm", action="store_true", help="também roda a leitura com LLM (usa LLM_API_KEY)")
    p.add_argument("--repetitions", type=int, default=1)
    p.add_argument("--model")
    p.add_argument("--output", type=Path, default=REPO_ROOT / "artifacts" / "evaluations")
    a = p.parse_args()
    result = asyncio.run(run(a.llm, a.repetitions, a.model))
    a.output.mkdir(parents=True, exist_ok=True)
    path = a.output / f"leitura-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    _print(result)
    print(f"\nresultado completo: {path}")


if __name__ == "__main__":
    main()
