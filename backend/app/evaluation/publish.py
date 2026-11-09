"""Publica uma execução real do benchmark para a demonstração e para a auditoria offline.

python -m app.evaluation.publish artifacts/evaluations/<execução>

Gera `published/summary.json` (cópia do resumo) e `published/measurements.json` (por execução: status, checks,
tokens por chamada, custo, tempo e o hash do trace). Não copia prompts, respostas, credenciais nem textos de erro.
Antes de gravar, recalcula tudo com `audit.recalculate` e confere contra o resumo: se não bater, não publica.
"""

import argparse
import hashlib
import json
from pathlib import Path

from app.evaluation.audit import PUBLISHED, recalculate, verify_summary
from app.evaluation.runner import dump

_USAGE_KEYS = (
    "model",
    "tokens_in",
    "tokens_out",
    "tokens_cached",
    "latency_ms",
    "retries",
    "prompt_chars",
    "usage_reported",
)


def _run_row(trace_path: Path) -> dict:
    raw = trace_path.read_bytes()
    run = json.loads(raw)
    return {
        "blind_id": run["blind_id"],
        "case_id": run["case_id"],
        "repetition": run["repetition"],
        "architecture": run["architecture"],
        "model_label": run["model_label"],
        "model": run["model"],
        "reasoning_effort": run.get("reasoning_effort"),
        "status": run["status"],
        "calls": run["calls"],
        "tokens_in": run["tokens_in"],
        "tokens_out": run["tokens_out"],
        "tokens_cached": run["tokens_cached"],
        "usage_complete": run["usage_complete"],
        "cost_usd": run["cost_usd"],
        "latency_seconds": run["latency_seconds"],
        "checks": run["automatic"]["checks"],
        "calls_detail": [
            {"ok": bool(c.get("ok")), "usage": {k: c["usage"][k] for k in _USAGE_KEYS if k in c["usage"]}}
            if c.get("ok")
            else {"ok": False}
            for c in run["trace"]
        ],
        "trace_sha256": hashlib.sha256(raw).hexdigest(),
    }


def build(run_dir: Path) -> tuple[dict, dict]:
    manifest = json.loads((run_dir / "manifest.json").read_text())
    summary_raw = (run_dir / "summary.json").read_bytes()
    summary = json.loads(summary_raw)
    runs = sorted((_run_row(p) for p in (run_dir / "traces").glob("*.json")), key=lambda r: r["blind_id"])
    measurements = {
        "version": 1,
        "source_run": run_dir.name,
        "source_summary_sha256": hashlib.sha256(summary_raw).hexdigest(),
        "app_sha256": manifest.get("app_sha256"),
        "source_app_hashes": manifest.get("source_app_hashes", [manifest.get("app_sha256")]),
        "measurement_revision": manifest.get("measurement_revision", "v3"),
        "cases": manifest["cases"],
        "models": manifest["models"],
        "schedule": manifest["schedule"],
        "runs": runs,
    }
    done = {(r["case_id"], r["architecture"], r["model_label"], r["repetition"]) for r in runs}
    if len(done) < len(manifest["schedule"]):
        # execução interrompida: publica só o que terminou e guarda o plano original para quem quiser comparar
        measurements["planned_schedule"] = manifest["schedule"]
        measurements["schedule"] = [s for s in manifest["schedule"] if tuple(s) in done]
    verify_summary(recalculate(measurements), summary)
    return measurements, summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--destination", type=Path, default=PUBLISHED)
    args = parser.parse_args()
    measurements, summary = build(args.run_dir)
    dump(args.destination / "measurements.json", measurements)
    dump(args.destination / "summary.json", summary)
    print(f"publicado: {len(measurements['runs'])} execuções de {args.run_dir.name}")


if __name__ == "__main__":
    main()
