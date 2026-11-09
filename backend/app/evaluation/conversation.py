"""Benchmark de conversa: mensagens fora do roteiro recebem resposta útil ou um "não tenho esse dado" honesto?

    python -m app.evaluation.conversation

Offline e determinístico: o assistente de entrada e o Bootstrap Resolver não usam LLM. Mede duas coisas:
- routing: o tipo de resposta e o conteúdo obrigatório/proibido de cada mensagem;
- lookup: como cada forma de escrever o nome do cliente é resolvida, inclusive o que NÃO pode casar.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

from app.config import Settings
from app.container import build_container
from app.core.schemas.case import DemoOptions
from app.governance.loader import load_identities
from app.orchestration.assist import assist

CASES_PATH = Path(__file__).with_name("conversation_cases.json")
ANALYST = "analyst-001"
OPEN_CASE = "O cliente Fazenda Horizonte S.A. solicita R$ 50 milhões para custeio da safra de soja 2025/26."


def load_cases() -> dict[str, Any]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


async def run() -> dict[str, Any]:
    data = load_cases()
    c = build_container(Settings(llm_api_key="", _env_file=None))
    user = load_identities()[ANALYST]
    state = (await c.orchestrator.create_case(ANALYST, OPEN_CASE, DemoOptions())).state

    routing = []
    for case in data["routing"]:
        reply = assist(case["text"], user, state if case.get("open_case") else None, c.knowledge, c.repo)
        body = " ".join([reply.message, *reply.bullets, *(f"{x.id} {x.title}" for x in reply.citations)])
        passed = (
            reply.kind == case["kind"]
            and all(s in body for s in case.get("contains", []))
            and not any(s in body for s in case.get("absent", []))
        )
        routing.append({"id": case["id"], "passed": passed, "kind": reply.kind, "expected": case["kind"]})

    lookup = []
    for case in data["lookup"]:
        r = c.resolver.resolve(user, case["ref"])
        got = r.client.client_id if r.client else None
        passed = r.status == case["status"] and got == case.get("client_id")
        lookup.append({"id": case["id"], "passed": passed, "status": r.status, "client_id": got})

    return {
        "version": data["version"],
        "routing": {"passed": sum(r["passed"] for r in routing), "total": len(routing), "rows": routing},
        "lookup": {"passed": sum(r["passed"] for r in lookup), "total": len(lookup), "rows": lookup},
    }


def main() -> None:
    out = asyncio.run(run())
    for name in ("routing", "lookup"):
        section = out[name]
        print(f"{name}: {section['passed']}/{section['total']}")
        for row in section["rows"]:
            if not row["passed"]:
                print("  falhou:", {k: v for k, v in row.items() if k != "passed"})


if __name__ == "__main__":
    main()
