"""Conversa de esclarecimento (ARCHITECTURE.md §3): o que perguntar de volta ao analista e como ler a resposta dele.

Perguntas saem do que a leitura da demanda NÃO encontrou; as bloqueantes vêm primeiro, os refinamentos (prazo,
garantias, tipo de operação) são opcionais. Respostas em texto livre viram `answers` UNTRUSTED — nunca permissão,
nunca escopo: um cliente citado na resposta continua passando pelo Bootstrap Resolver e pelo CaseScope.
"""

import re
from typing import Any

from app.core.crops import expected_price_unit, normalize_crop
from app.core.schemas.case import OpenQuestion
from app.core.schemas.outputs import InterpretedDemand
from app.orchestration.interpreter import _fmt, heuristic_interpret, parse_amount

REQUEST_KIND_LABEL = {
    "nova_operacao": "operação nova",
    "renovacao": "renovação",
    "aumento_de_limite": "aumento de limite",
}

# rótulo legível de cada chave de contexto que vai para a squad (answers)
CONTEXT_LABEL = {
    "prazo_desejado_meses": "Prazo desejado",
    "garantias_oferecidas": "Garantias oferecidas",
    "tipo_de_operacao": "Tipo de operação",
    "regiao": "Região",
    "area_hectares": "Área",
    "observacoes_do_analista": "Observações",
}


# O LLM às vezes registra como ambiguidade um campo só ausente ("Qual é a região?"). Isso não é dúvida de leitura, e as
# perguntas padrão abaixo já cobrem o que importa. "O pedido é para X ou Y?" e "Confirme a cultura: ..." continuam.
_MISSING_FIELD_QUESTION = re.compile(
    r"^\s*(?:qual|quais|h[aá]|existe|existem)\b[^?]*\b"
    r"(?:safra|regi[aã]o|[aá]rea|garantias?|prazo|cultura|tipo de opera[cç][aã]o|finalidade|valor)\b[^?]*\?\s*$",
    re.IGNORECASE,
)


def open_questions(it: InterpretedDemand | None) -> list[OpenQuestion]:
    if it is None:
        return []
    qs: list[OpenQuestion] = [
        OpenQuestion(key=f"confirmacao_{i + 1}", question=q, blocking=False)
        for i, q in enumerate(a for a in it.ambiguities if not _MISSING_FIELD_QUESTION.search(a))
    ]
    if it.requested_amount is None:
        qs.append(OpenQuestion(key="requested_amount", question="Qual o valor que o cliente está pedindo?", blocking=True))
    if it.purpose is None:
        qs.append(
            OpenQuestion(
                key="purpose", question="O recurso é para custeio, investimento ou comercialização?", blocking=False
            )
        )
    if it.crop is None or it.cycle is None:
        ask = "Qual safra (por exemplo, 2026/27)?" if it.crop else "Qual cultura e qual safra (por exemplo, soja 2025/26)?"
        qs.append(OpenQuestion(key="crop_cycle", question=ask))
    if it.request_kind is None:
        qs.append(
            OpenQuestion(
                key="tipo_de_operacao", question="É uma operação nova ou a renovação de uma linha que o cliente já tem?"
            )
        )
    if it.tenor_months is None:
        qs.append(
            OpenQuestion(
                key="prazo_desejado_meses",
                question="Existe um prazo desejado? Sem isso, a squad alinha o pagamento ao ciclo da safra.",
            )
        )
    if not it.guarantees:
        qs.append(
            OpenQuestion(
                key="garantias_oferecidas",
                question="Quais garantias o cliente oferece? Sem isso, a squad considera as garantias usuais do produto.",
            )
        )
    return qs


def demand_warnings(
    it: InterpretedDemand | None, profile_crop: str | None, market: dict[str, Any] | None, min_amount: float
) -> list[OpenQuestion]:
    """O que já se sabe que vai parar na Elegibilidade. Antecipa, não relaxa: a regra continua sendo do gate."""
    if it is None:
        return []
    out: list[OpenQuestion] = []
    crop = it.crop
    if crop and profile_crop and normalize_crop(profile_crop) != normalize_crop(crop):
        out.append(
            OpenQuestion(
                key="aviso_cultura",
                question=f"A base de demonstração tem perfil de {profile_crop} para este cliente, não de {crop}. "
                f"Assim, a análise para na Elegibilidade antes dos cálculos. A cultura é {crop} mesmo ou {profile_crop}?",
            )
        )
    if crop and market is None:
        out.append(
            OpenQuestion(
                key="aviso_mercado",
                question=f"Não há cotação de {crop} na base de mercado da demonstração, e a análise não usa o preço de "
                "outra cultura. Se rodar assim, ela para na Elegibilidade.",
            )
        )
    elif crop and market is not None and market.get("unit") != expected_price_unit(crop):
        out.append(
            OpenQuestion(
                key="aviso_mercado",
                question=f"A cotação de {crop} está em {market.get('unit')}, e os cálculos usam "
                f"{expected_price_unit(crop)}. "
                "Se rodar assim, a análise para na Elegibilidade.",
            )
        )
    if it.requested_amount is not None and it.requested_amount < min_amount:
        out.append(
            OpenQuestion(
                key="aviso_ticket",
                question=f"O valor de {_fmt('requested_amount', it.requested_amount)} está abaixo do ticket mínimo da "
                f"política ({_fmt('requested_amount', min_amount)}). Confirme se o valor está certo.",
            )
        )
    return out


def context_from(h: InterpretedDemand) -> dict[str, Any]:
    """Refinamentos reconhecidos num texto (prazo, garantias, tipo, região, área) como answers para a squad."""
    out: dict[str, Any] = {}
    if h.tenor_months:
        out["prazo_desejado_meses"] = h.tenor_months
    if h.guarantees:
        out["garantias_oferecidas"] = ", ".join(h.guarantees)
    if h.request_kind:
        out["tipo_de_operacao"] = REQUEST_KIND_LABEL.get(h.request_kind, h.request_kind)
    if h.region:
        out["regiao"] = h.region
    if h.area_hectares:
        out["area_hectares"] = h.area_hectares
    return out


_LEAD_IN = re.compile(
    r"^\s*(?:(?:o|a)\s+)?(?:cliente\s+(?:é|e)\s+|é\s+(?:o|a)\s+|e\s+(?:o|a)\s+|trata-se\s+d[oa]\s+|se\s+chama\s+)",
    re.IGNORECASE,
)


def client_from_text(text: str) -> str:
    """Nome/ID do cliente numa resposta curta ("é a Fazenda Horizonte S.A."). O Resolver decide se existe e se pode."""
    h = heuristic_interpret(text)
    if h.client_ref:
        return h.client_ref
    name = _LEAD_IN.sub("", text.strip()).strip()
    # tira o ponto final da frase, mas não o de abreviações como "S.A."
    return re.sub(r"(?<![A-Z]\.[A-Z])(?<![Ll]tda)(?<!LTDA)\.$", "", name).strip() or text.strip()


def answers_from_text(text: str, items: list[str]) -> dict[str, Any]:
    """Resposta livre a um pedido de informação → answers por item pedido + refinamentos reconhecidos."""
    answers: dict[str, Any] = {}
    h = heuristic_interpret(text)
    for item in items:
        if item in ("client_ref", "client_id"):
            answers[item] = client_from_text(text)
        elif item == "requested_amount":
            if (amount := parse_amount(text, bare_number_ok=True)) is not None:
                answers[item] = amount
        elif item == "crop":
            answers[item] = h.crop or text.strip()
        else:
            answers[item] = text.strip()
    for key in ("purpose", "crop", "cycle"):
        if (value := getattr(h, key)) and key not in answers:
            answers[key] = value
    return answers | {k: v for k, v in context_from(h).items() if k not in answers}


def describe_context(answers: dict[str, Any]) -> dict[str, str]:
    """Contexto do analista em texto legível para a UI (só o que ele mesmo escreveu ou o que foi reconhecido)."""
    out: dict[str, str] = {}
    for key, label in CONTEXT_LABEL.items():
        if key not in answers or answers[key] in (None, ""):
            continue
        value = answers[key]
        if key == "prazo_desejado_meses":
            value = f"{value} meses"
        elif key == "area_hectares":
            value = f"{float(value):,.0f} ha".replace(",", ".")
        out[label] = str(value)
    return out
