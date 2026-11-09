"""Detector de dados pessoais e financeiros (DLP leve, determinístico).

Roda em três pontos: no Gateway (antes de o registro virar evidência e chegar ao LLM), nos anexos do analista e no
Output Guard (último filtro do relatório). Só redige o que tem formato E validação: CPF e CNPJ com dígito verificador,
cartão com Luhn, e-mail, telefone brasileiro formatado, agência/conta e chave PIX com palavra de contexto. IDs do
sistema (CLIENTE-001), valores em reais, datas e safras não são tocados.
"""

import re
from collections.abc import Callable

REDACTED = "[DADO PESSOAL REMOVIDO]"

_CPF = re.compile(r"(?<![\d./-])\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?![\d./-])")
_CNPJ = re.compile(r"(?<![\d./-])\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}(?![\d./-])")
_CARD = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_PHONE = re.compile(r"(?:\+?55\s?)?(?:\(\d{2}\)\s?|\b\d{2}\s)9?\d{4}-?\d{4}\b|\+55\s?\d{2}\s?9?\d{4}-?\d{4}\b")
_BANK = re.compile(
    r"\b(ag[eê]ncia|ag\.?|conta(?:\s+corrente|\s+poupan[cç]a)?|c/c|cc)\s*(?:n[ºo°.]?\s*)?:?\s*(\d{3,6}(?:-?[\dxX])?)\b",
    re.IGNORECASE,
)
_PIX = re.compile(
    r"\b(chave\s+)?pix\b[^\n]{0,20}?:?\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.IGNORECASE
)


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def valid_cpf(raw: str) -> bool:
    d = _digits(raw)
    if len(d) != 11 or d == d[0] * 11:
        return False
    for size in (9, 10):
        total = sum(int(d[i]) * (size + 1 - i) for i in range(size))
        if (total * 10 % 11) % 10 != int(d[size]):
            return False
    return True


def valid_cnpj(raw: str) -> bool:
    d = _digits(raw)
    if len(d) != 14 or d == d[0] * 14:
        return False
    for size, weights in ((12, [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]), (13, [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])):
        rest = sum(int(d[i]) * weights[i] for i in range(size)) % 11
        if (0 if rest < 2 else 11 - rest) != int(d[size]):
            return False
    return True


def valid_card(raw: str) -> bool:
    d = _digits(raw)
    if not 13 <= len(d) <= 19 or d[0] not in "23456":
        return False
    total = 0
    for i, ch in enumerate(reversed(d)):
        n = int(ch) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def redact(text: str, bump: Callable[[str], None] | None = None) -> tuple[str, dict[str, int]]:
    """Devolve o texto redigido e a contagem por tipo. `bump(tipo)` é chamado a cada redação, se dado."""
    counts: dict[str, int] = {}

    def hit(kind: str) -> None:
        counts[kind] = counts.get(kind, 0) + 1
        if bump is not None:
            bump(kind)

    def checked(kind: str, validator: Callable[[str], bool]) -> Callable[[re.Match[str]], str]:
        def sub(m: re.Match[str]) -> str:
            if validator(m.group(0)):
                hit(kind)
                return REDACTED
            return m.group(0)

        return sub

    text = _CNPJ.sub(checked("cnpj", valid_cnpj), text)
    text = _CPF.sub(checked("cpf", valid_cpf), text)
    text = _CARD.sub(checked("card", valid_card), text)
    for kind, pat in (("email", _EMAIL), ("phone", _PHONE)):
        text, n = pat.subn(REDACTED, text)
        for _ in range(n):
            hit(kind)

    def keep_label(kind: str) -> Callable[[re.Match[str]], str]:
        def sub(m: re.Match[str]) -> str:
            hit(kind)
            return m.group(0)[: m.start(2) - m.start(0)] + REDACTED

        return sub

    text = _BANK.sub(keep_label("bank_account"), text)
    text = _PIX.sub(keep_label("pix_key"), text)
    return text, counts


def redact_value(value, counts: dict[str, int]):
    """Redige recursivamente strings dentro de dicts/listas, acumulando a contagem."""
    if isinstance(value, str):
        out, c = redact(value)
        for k, n in c.items():
            counts[k] = counts.get(k, 0) + n
        return out
    if isinstance(value, dict):
        return {k: redact_value(v, counts) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v, counts) for v in value]
    return value
