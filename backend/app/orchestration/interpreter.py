"""Interpretação da demanda (ARCHITECTURE.md §3). Saída é PROPOSTA: nada aqui vira permissão.

Duas leituras independentes do mesmo pedido:
- `heuristic_interpret`: determinística (regex + vocabulário). Funciona sem chave e é a rede de segurança.
- `interpret`: o LLM lê o pedido inteiro, explica como chegou em cada campo e aponta ambiguidades. Cada campo que ele
  propõe passa por validação (formato e ancoragem no texto). Onde as duas leituras divergem em algo material, a
  divergência vira pergunta ao analista em vez de um palpite silencioso.
O prompt do usuário entra como UNTRUSTED_DATA; falha do LLM → heurística.
"""

import re
import unicodedata
from datetime import date
from typing import Any

from pydantic import ValidationError

from app.core.schemas.outputs import DemandReading, InterpretedDemand
from app.llm.openai_compat import LLMError
from app.llm.prompting import UNTRUSTED_RULES, extract_json, render_schema, wrap_untrusted
from app.llm.provider import LLMProvider, Message

_CLIENT_ID_RE = re.compile(r"\bCLIENTE-\d{3,}\b", re.IGNORECASE)
# "cliente Fazenda Horizonte S.A. solicita" / "empresa X pede" / "para a Agro Delta Ltda."
# razão social sem "cliente" antes: "Fazenda Horizonte S.A. pediu", "da fazenda horizonte sa", "Agropecuária Boa Vista"
_BUSINESS_RE = re.compile(
    r"\b(?P<name>(?:fazendas?|agropecu[aá]ria|agr[ií]cola|agroindustrial|s[ií]tio|cooperativa|grupo|usina|cerealista)"
    r"(?:\s+[\w&.\-/]+){1,4})",
    re.IGNORECASE,
)
_NAME_STOP = {
    "solicita",
    "solicitou",
    "pede",
    "pediu",
    "precisa",
    "precisam",
    "quer",
    "querem",
    "deseja",
    "busca",
    "requer",
    "ligou",
    "esta",
    "está",
    "tem",
    "vai",
    "para",
    "pra",
    "pro",
    "de",
    "com",
    "no",
    "na",
    "em",
    "e",
    "que",
    "a",
    "o",
    "um",
    "uma",
    "r$",
    "safra",
    "custeio",
    "investimento",
    "comercializacao",
    "comercialização",
    "renovar",
    "renovação",
}
_LEGAL_SUFFIX = {"s.a.", "s.a", "sa", "s/a", "ltda", "ltda.", "eireli", "me", "epp"}
_CLIENT_NAME_RE = re.compile(
    r"\b(?:cliente|empresa|produtor|grupo)\s+(?P<name>[A-ZÁ-Ú][\w&.\-]*(?:\s+[A-ZÁ-Úa-zá-ú][\w&.\-]*){0,5}?)"
    r"(?=\s+(?:solicita|pede|precisa|quer|deseja|busca|requer)|[,.;:]|\s*$)",
)
_AMOUNT_RE = re.compile(
    r"R?\$?\s*(?P<num>\d{1,3}(?:[.\s]\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>milh(?:ão|ões|oes|ao)|mi\b|mm\b|m\b|bilh(?:ão|ões|oes|ao)|bi\b|mil\b|k\b)?",
    re.IGNORECASE,
)
# safra: 2025/26, 2025/2026, 25/26 (o segundo ano precisa ser o seguinte ao primeiro)
_CYCLE_RE = re.compile(r"(?<![\d/])(?:20)?(\d{2})\s*/\s*(?:20)?(\d{2})(?![\d/])")
_TENOR_MONTHS_RE = re.compile(r"\b(\d{1,3})\s*(?:meses|m[eê]s)\b", re.IGNORECASE)
_TENOR_YEARS_RE = re.compile(r"\b(\d{1,2})\s*anos?\b", re.IGNORECASE)
_AREA_RE = re.compile(r"\b(\d{1,3}(?:[.\s]\d{3})+|\d+(?:,\d+)?)\s*(?:ha|hectares)\b", re.IGNORECASE)
_GUARANTEES: tuple[tuple[str, str], ...] = (
    (r"penhor", "penhor da safra"),
    (r"aliena[cç][aã]o\s+fiduci", "alienação fiduciária"),
    (r"\baval", "aval dos sócios"),
    (r"hipoteca", "hipoteca"),
    (r"\bCPR\b", "CPR"),
    (r"cess[aã]o\s+de\s+receb", "cessão de recebíveis"),
    (r"seguro\s+(?:agr[ií]cola|rural)", "seguro agrícola"),
    (r"fian[cç]a", "fiança"),
)
_REQUEST_KINDS: tuple[tuple[str, str], ...] = (
    (r"renova", "renovacao"),
    (r"aument|amplia", "aumento_de_limite"),
    (r"nova\s+opera|primeira\s+opera|opera[cç][aã]o\s+nova", "nova_operacao"),
)
# (nome por extenso, sigla, rótulo). A sigla só vale em maiúsculas ("MT"), para não confundir com palavras comuns.
_REGIONS: tuple[tuple[str, str, str], ...] = (
    (r"mato\s+grosso\s+do\s+sul", "MS", "Mato Grosso do Sul"),
    (r"mato\s+grosso(?!\s+do\s+sul)", "MT", "Mato Grosso"),
    (r"goi[aá]s", "GO", "Goiás"),
    (r"paran[aá]", "PR", "Paraná"),
    (r"rio\s+grande\s+do\s+sul", "RS", "Rio Grande do Sul"),
    (r"bahia", "BA", "Bahia"),
    (r"minas\s+gerais", "MG", "Minas Gerais"),
    (r"tocantins", "TO", "Tocantins"),
    (r"matopiba", "", "Matopiba"),
)
_PURPOSES = ("custeio", "investimento", "comercializacao", "comercialização", "industrializacao", "industrialização")
# sinônimos de finalidade (testados depois das palavras exatas acima)
_PURPOSE_HINTS: tuple[tuple[str, str], ...] = (
    (r"plantar|plantio|insumos|custear|adubo|fertilizante|defensivo|semente|tocar a safra", "custeio"),
    (
        r"m[aá]quina|trator|colheitadeira|plantadeira|armaz[eé]m|silo|piv[oô]|irriga[cç][aã]o|galp[aã]o|"
        r"investir|expans[aã]o d[ae] [aá]rea|abertura de [aá]rea|corre[cç][aã]o de solo",
        "investimento",
    ),
    (
        r"estocar|estocagem|segurar a venda|vender (?:a|o) (?:safra|produ)|pr[eé]-comercializa|"
        r"warrant|financiar a venda",
        "comercializacao",
    ),
)
_CROPS = ("soja", "milho", "algodao", "algodão", "cana", "cafe", "café", "trigo", "arroz", "feijao", "feijão", "sorgo")
_CROP_HINTS: tuple[tuple[str, str], ...] = ((r"safrinha", "milho"), (r"cana-de-a[cç][uú]car", "cana"))

_UNITS = {
    "um": 1,
    "uma": 1,
    "dois": 2,
    "duas": 2,
    "tres": 3,
    "três": 3,
    "quatro": 4,
    "cinco": 5,
    "seis": 6,
    "sete": 7,
    "oito": 8,
    "nove": 9,
    "dez": 10,
    "onze": 11,
    "doze": 12,
    "treze": 13,
    "quatorze": 14,
    "catorze": 14,
    "quinze": 15,
    "dezesseis": 16,
    "dezessete": 17,
    "dezoito": 18,
    "dezenove": 19,
    "vinte": 20,
    "trinta": 30,
    "quarenta": 40,
    "cinquenta": 50,
    "sessenta": 60,
    "setenta": 70,
    "oitenta": 80,
    "noventa": 90,
    "cem": 100,
    "cento": 100,
    "duzentos": 200,
    "trezentos": 300,
    "quatrocentos": 400,
    "quinhentos": 500,
    "seiscentos": 600,
    "setecentos": 700,
    "oitocentos": 800,
    "novecentos": 900,
}
_NUMWORD = "|".join(sorted(_UNITS, key=len, reverse=True))
_WORD_AMOUNT_RE = re.compile(
    rf"\b(?P<words>(?:{_NUMWORD})(?:\s+e\s+(?:{_NUMWORD}))*)\s+(?P<unit>milh(?:ão|ões|oes|ao)|bilh(?:ão|ões|oes|ao)|mil)"
    r"(?P<half>\s+e\s+meio)?\b",
    re.IGNORECASE,
)


def _words_to_digits(text: str) -> str:
    """'cinquenta milhões' → '50 milhões'; 'um milhão e meio' → '1,5 milhão'. Só antes de unidade monetária."""

    def repl(m: re.Match[str]) -> str:
        n = sum(_UNITS[w.lower()] for w in re.split(r"\s+e\s+", m.group("words")))
        num = f"{n},5" if m.group("half") else str(n)
        return f"{num} {m.group('unit')}"

    return _WORD_AMOUNT_RE.sub(repl, text)


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _business_name(text: str) -> str | None:
    """Razão social a partir da palavra-chave (Fazenda, Agropecuária...) até o primeiro verbo/preposição."""
    m = _BUSINESS_RE.search(text)
    if not m:
        return None
    words: list[str] = []
    for raw in m.group("name").split():
        w = raw.rstrip(",;:!?")
        if words and (w.lower() in _NAME_STOP or re.match(r"^\d", w)):
            break
        words.append(w)
        if w.lower() in _LEGAL_SUFFIX:
            break
        if raw != w:  # pontuação depois da palavra fecha o nome
            break
    if words and words[-1].endswith(".") and words[-1].lower() not in _LEGAL_SUFFIX:
        words[-1] = words[-1].rstrip(".")
    return " ".join(words) if len(words) >= 2 else None


def _cycle(text: str) -> str | None:
    for m in _CYCLE_RE.finditer(text):
        a, b = int(m.group(1)), int(m.group(2))
        if 15 <= a <= 60 and b == (a + 1) % 100:
            return f"20{a:02d}/{b:02d}"
    return None


def _to_number(raw: str) -> float:
    raw = raw.replace(" ", "")
    if "," in raw:  # 1.234,56 ou 1,5
        return float(raw.replace(".", "").replace(",", "."))
    if raw.count(".") == 1 and len(raw.split(".")[1]) != 3:  # 1.5 (decimal), não 50.000
        return float(raw)
    return float(raw.replace(".", ""))


def parse_amount(text: str, *, bare_number_ok: bool = False) -> float | None:
    """Valor em reais a partir de texto livre. Em prosa, número sem R$/unidade é ignorado (ano, área, sc/ha);
    `bare_number_ok` é para campos que só contêm o valor (ex.: resposta do formulário)."""
    best: float | None = None
    text = _words_to_digits(text)
    for m in _AMOUNT_RE.finditer(text):
        raw, unit = m.group("num"), (m.group("unit") or "").lower()
        has_currency = m.group(0).lstrip().startswith(("R$", "$"))
        if not unit and not has_currency and not bare_number_ok:
            continue
        mult = 1.0
        if unit.startswith("milh") or unit in ("mi", "mm", "m"):
            mult = 1e6
        elif unit.startswith("bilh") or unit == "bi":
            mult = 1e9
        elif unit in ("mil", "k"):
            mult = 1e3
        value = _to_number(raw) * mult
        if best is None or value > best:
            best = value
    return best


def heuristic_interpret(prompt: str) -> InterpretedDemand:
    text = prompt.strip()
    lower = _strip_accents(text.lower())

    client_ref: str | None = None
    if m := _CLIENT_ID_RE.search(text):
        client_ref = m.group(0).upper()
    elif m := _CLIENT_NAME_RE.search(text):
        client_ref = m.group("name").strip()
    else:
        client_ref = _business_name(text)

    purpose = next((p for p in _PURPOSES if p in lower), None) or next(
        (p for pat, p in _PURPOSE_HINTS if re.search(pat, lower)), None
    )
    crop = next((c for c in _CROPS if re.search(rf"\b{_strip_accents(c)}\b", lower)), None) or next(
        (c for pat, c in _CROP_HINTS if re.search(pat, lower)), None
    )
    cycle = _cycle(text)

    tenor: int | None = None
    if m := _TENOR_MONTHS_RE.search(text):
        tenor = int(m.group(1))
    elif m := _TENOR_YEARS_RE.search(text):
        tenor = int(m.group(1)) * 12
    area = _to_number(m.group(1)) if (m := _AREA_RE.search(text)) else None

    return InterpretedDemand(
        intent="credito_agro",
        client_ref=client_ref,
        requested_amount=parse_amount(text),
        purpose=_strip_accents(purpose) if purpose else None,
        crop=_strip_accents(crop) if crop else None,
        cycle=cycle,
        notes="heuristic",
        request_kind=next((k for pat, k in _REQUEST_KINDS if re.search(pat, lower)), None),
        tenor_months=tenor,
        guarantees=[g for pat, g in _GUARANTEES if re.search(pat, text, re.IGNORECASE)],
        region=next(
            (label for name, uf, label in _REGIONS if re.search(name, lower) or (uf and re.search(rf"\b{uf}\b", text))),
            None,
        ),
        area_hectares=area,
    )


# ------------------------------------------------------------------ leitura pelo LLM

_READ_SYSTEM = (
    """\
Você é o analista sênior que faz a triagem de pedidos de crédito agro antes de a squad trabalhar. \
O pedido chega do jeito que o gerente escreveu: informal, abreviado, com números por extenso, com erros de digitação, \
às vezes com outros valores no meio (dívida, faturamento, área) que NÃO são o valor pedido.

Leia o pedido inteiro e raciocine antes de preencher os campos:
1. Quem é o cliente? Copie o nome ou código exatamente como está no texto (não complete razão social, não invente).
2. Quanto está sendo pedido? Separe o valor do pedido de outros valores citados (dívida, faturamento, limite atual). \
"50M", "50 mi", "cinquenta milhões" = 50000000. Em aumento de limite, o valor pedido é o novo total, se dito; \
senão, o acréscimo.
3. Para quê? custeio (insumos, plantio, tocar a safra), investimento (máquinas, armazéns, irrigação, área), \
comercializacao (estocar, vender depois, pré-comercialização) ou industrializacao.
4. Qual cultura e qual safra (AAAA/AA)? "safrinha" é milho de segunda safra. Safra relativa ("próxima safra", \
"essa safra") deve ser convertida pela data de hoje e também registrada como ambiguidade.
5. É operação nova, renovação ou aumento de limite? Há prazo, garantias, região ou área?
6. O que ficou ambíguo ou faltando? Escreva cada ponto como uma pergunta curta e objetiva ao analista.

Campo que o texto não sustenta fica null. Não decide crédito, não aprova, não infere permissões.

"""
    + UNTRUSTED_RULES
)

_MATERIAL = ("client_ref", "requested_amount", "purpose", "crop", "cycle")
_CONTEXT = ("request_kind", "tenor_months", "guarantees", "region", "area_hectares")
_KNOWN_CROPS = {_strip_accents(c) for c in _CROPS}
_PURPOSE_SET = {"custeio", "investimento", "comercializacao", "industrializacao"}


def _tokens(s: str) -> list[str]:
    return re.sub(r"[^\w\s]", " ", _strip_accents(s.casefold())).split()


def _client_core(s: str) -> str:
    toks = [t for t in _tokens(s) if t not in {"s", "a", "sa", "ltda", "eireli", "me", "epp"}]
    return " ".join(toks)


def _grounded_client(ref: str, text: str) -> bool:
    """O nome proposto precisa estar no texto: todas as palavras significativas aparecem no pedido."""
    if _CLIENT_ID_RE.fullmatch(ref.strip()):
        return ref.strip().upper() in text.upper()
    words = set(_tokens(text))
    sig = [t for t in _tokens(ref) if len(t) >= 3 and t not in {"ltda", "eireli"}]
    return bool(sig) and all(t in words for t in sig)


def _has_number(text: str) -> bool:
    return bool(re.search(r"\d", text) or _WORD_AMOUNT_RE.search(text))


def validate_reading(reading: DemandReading, text: str) -> tuple[dict[str, Any], list[str]]:
    """Campos do LLM que passam na validação + motivos dos descartados (vão para o raciocínio, não para o analista)."""
    out: dict[str, Any] = {}
    dropped: list[str] = []
    if reading.client_ref:
        if _grounded_client(reading.client_ref, text):
            out["client_ref"] = reading.client_ref.strip()
        else:
            dropped.append(f"cliente '{reading.client_ref}' não aparece no texto")
    if reading.requested_amount is not None:
        if reading.requested_amount > 0 and _has_number(text):
            out["requested_amount"] = float(reading.requested_amount)
        else:
            dropped.append("valor sem número correspondente no texto")
    if reading.purpose in _PURPOSE_SET:
        out["purpose"] = reading.purpose
    if reading.crop:
        crop = _strip_accents(reading.crop.strip().lower())
        if crop in _KNOWN_CROPS:
            out["crop"] = crop
        else:
            dropped.append(f"cultura '{reading.crop}' fora do vocabulário")
    if reading.cycle:
        if m := re.fullmatch(r"(20\d{2})\s*/\s*(?:20)?(\d{2})", reading.cycle.strip()):
            a, b = int(m.group(1)), int(m.group(2))
            if b == (a + 1) % 100:
                out["cycle"] = f"{a}/{b:02d}"
        if "cycle" not in out:
            dropped.append(f"safra '{reading.cycle}' em formato inválido")
    if reading.request_kind:
        out["request_kind"] = reading.request_kind
    if reading.tenor_months and 1 <= reading.tenor_months <= 240:
        out["tenor_months"] = reading.tenor_months
    if reading.guarantees:
        out["guarantees"] = [g.strip() for g in reading.guarantees if g.strip()][:8]
    if reading.region:
        out["region"] = reading.region.strip()
    if reading.area_hectares and reading.area_hectares > 0:
        out["area_hectares"] = float(reading.area_hectares)
    return out, dropped


def _same(field: str, a: Any, b: Any) -> bool:
    if field == "requested_amount":
        return abs(float(a) - float(b)) < 0.5
    if field == "client_ref":
        return _client_core(str(a)) == _client_core(str(b))
    return _strip_accents(str(a).lower()) == _strip_accents(str(b).lower())


_LABEL = {
    "client_ref": "o cliente",
    "requested_amount": "o valor",
    "purpose": "a finalidade",
    "crop": "a cultura",
    "cycle": "a safra",
}


def _fmt(field: str, v: Any) -> str:
    if field == "requested_amount":
        return f"R$ {float(v):,.0f}".replace(",", ".")
    return str(v)


def reconcile(base: InterpretedDemand, llm: dict[str, Any], reading: DemandReading, dropped: list[str]) -> InterpretedDemand:
    """Junta as duas leituras. LLM vence onde entendeu o texto; regex vence em código explícito de cliente;
    divergência material vira pergunta (ambiguidade) em vez de palpite silencioso."""
    update: dict[str, Any] = {}
    ambiguities = [a.strip() for a in reading.ambiguities if a and a.strip()][:4]
    for field in _MATERIAL:
        h, m = getattr(base, field), llm.get(field)
        if m is None:
            continue
        if h is not None and not _same(field, h, m):
            explicit_id = field == "client_ref" and _CLIENT_ID_RE.fullmatch(str(h))
            chosen = h if explicit_id else m
            other = m if explicit_id else h
            update[field] = chosen
            ambiguities.append(
                f"Confirme {_LABEL[field]}: entendi {_fmt(field, chosen)}, mas o texto também cita {_fmt(field, other)}."
            )
        else:
            update[field] = m
    for field in _CONTEXT:
        if field in llm and not getattr(base, field):
            update[field] = llm[field]
    used = sorted(k for k in update if getattr(base, k) != update[k])
    reasoning = reading.reasoning.strip()
    if dropped:
        reasoning += ("\n" if reasoning else "") + "Descartado na validação: " + "; ".join(dropped) + "."
    return base.model_copy(
        update=update
        | {
            "notes": "llm" + (":" + ",".join(used) if used else "") + "+heuristic",
            "reasoning": reasoning[:1500],
            "ambiguities": list(dict.fromkeys(ambiguities)),
        }
    )


async def interpret(
    prompt: str, provider: LLMProvider | None, model: str, *, today: date | None = None
) -> InterpretedDemand:
    base = heuristic_interpret(prompt)
    if provider is None:
        return base
    today = today or date.today()
    user = (
        f"Data de hoje: {today.isoformat()}.\n\n## Pedido do gerente\n"
        + wrap_untrusted("user_prompt", prompt)
        + "\n\n## Formato de saída\nJSON conforme o schema abaixo, começando pelo campo reasoning:\n"
        + render_schema(DemandReading)
    )
    try:
        resp = await provider.complete(
            model=model,
            messages=[Message(role="system", content=_READ_SYSTEM), Message(role="user", content=user)],
            response_schema=DemandReading,
        )
        reading = DemandReading.model_validate(extract_json(resp.content or ""))
    except (LLMError, ValueError, ValidationError):
        return base
    llm, dropped = validate_reading(reading, prompt)
    return reconcile(base, llm, reading, dropped)
