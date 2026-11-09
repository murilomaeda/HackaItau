"""Injection Guard (ARCHITECTURE.md §11.3): heurístico, roda no Gateway sobre cada registro. Marca; não bloqueia.

Não é a barreira (o LLM não tem capability a escalar, e scope + policy negam qualquer acesso), mas precisa ver através
dos disfarces comuns antes de procurar os padrões:

1. Unicode: NFKC, sem acentos, sem caracteres invisíveis (zero-width, bidi), homóglifos cirílicos/gregos → latinos;
2. variantes: leetspeak (1gn0r3), letras espaçadas (i g n o r e), texto em base64 decodificado;
3. padrões com peso por categoria (instrução, papel, exfiltração, marcadores de chat, ação); pontuação ≥ 1 marca.

Também procura IDs de cliente fora do scope em todas as variantes ("CLlENTE-999" com L no lugar de I conta).
"""

import base64
import binascii
import re
import unicodedata
from typing import Any

from pydantic import BaseModel, Field

from app.core.schemas.context import CaseScope

# tolera letras trocadas por parecidas (l/1/| no lugar de i) e espaços entre as letras e os dígitos
CLIENT_REF_RE = re.compile(r"\bc\s*[l1|]\s*[i1l|!]\s*e\s*n\s*t\s*e\s*-\s*((?:\d\s*){3,})", re.IGNORECASE)

_INVISIBLE = re.compile("[­͏؜ᅟᅠ឴឵᠎​-‏‪-‮⁠-⁤﻿]")
_HOMOGLYPHS = str.maketrans(
    {
        # cirílico
        "а": "a", "в": "b", "е": "e", "ѕ": "s", "і": "i", "ј": "j", "к": "k", "м": "m", "н": "h", "о": "o",
        "р": "p", "с": "c", "т": "t", "у": "y", "х": "x", "ԁ": "d", "ɡ": "g", "ӏ": "l",
        # grego
        "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "u", "χ": "x",
    }
)  # fmt: skip
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "|": "l"})
_SPACED = re.compile(r"\b(?:\w[ .\-_]){3,}\w\b")
_B64 = re.compile(r"[A-Za-z0-9+/]{24,}={0,2}")

# (categoria, peso, padrão sobre o texto normalizado: minúsculo, sem acento)
_RULES: tuple[tuple[str, float, str], ...] = (
    ("override_instructions", 1.0, r"\b(ignore|ignora|desconsidere|esqueca|forget|disregard|override|sobrescrev\w*)\w*\s+"
     r"(?:\w+\s+){0,3}(instruc|previous|prior|anterior|regras|rules|politica|policy|guardrail|context)"),
    ("override_instructions", 1.0, r"\b(novas|new|updated|atualizadas)\s+(instrucoes|instructions|regras|rules)\b"),
    ("override_instructions", 0.6, r"\b(a partir de agora|from now on|de agora em diante)\b"),
    ("system_prompt", 1.0, r"system\s*prompt|prompt\s+(do\s+)?sistema|instrucoes\s+(do\s+)?sistema"),
    ("role_change", 1.0, r"\b(voce|you)\s+(agora\s+|now\s+)?(e|eh|is|are)\s+(o\s+|a\s+|the\s+|an?\s+)?"
     r"(admin|administrador|administrator|root|desenvolvedor|developer|dan|sistema|system)\b"),
    ("role_change", 1.0, r"\b(modo|mode)\s+(desenvolvedor|developer|dan|jailbreak|deus|god)\b|\bjailbreak\b"),
    ("role_change", 0.5, r"\b(aja|atue|act|finja|pretend)\s+(como|as|que|to be)\b"),
    ("chat_markup", 1.0, r"<\|?(im_start|im_end|system|endoftext)\|?>|\[/?inst\]|<<sys>>"),
    ("chat_markup", 0.5, r"(^|\n)\s*(#{2,}\s*)?(system|assistant|sistema)\s*:"),
    ("scope_escape", 1.0, r"\b(consulte|acesse|busque|leia|mostre|use|access|fetch|query)\s+(?:\w+\s+){0,4}"
     r"cliente\s*-\s*\d+"),
    ("decision_forcing", 1.0, r"\b(aprove|aprovar|approve|libere|liberar)\s+(?:\w+\s+){0,2}(credito|credit|imediatamente"
     r"|now|operacao|emprestimo|loan)\b"),
    ("decision_forcing", 0.6, r"\b(classifique|classify|marque|mark)\s+(?:\w+\s+){0,3}"
     r"(baixo risco|low risk|sem risco|aprovad)"),
    ("secret_exfiltration", 1.0, r"\b(revele|reveal|exiba|mostre|print|imprima|vaze|leak|show)\s+(?:\w+\s+){0,3}"
     r"(senha|password|secret|segredo|api[_\s-]?key|chave|token|prompt|credencia)"),
    ("data_exfiltration", 1.0, r"\b(envie|mande|send|post|exfiltr\w*|encaminhe|forward)\s+(?:\w+\s+){0,5}"
     r"(para|to)\s+(https?://|www\.|\S+@\S+\.\w+)"),
    ("data_exfiltration", 0.5, r"!\[[^\]]*\]\(https?://[^)]*[?&][^)]*\)"),  # imagem markdown com dados na URL
    ("tool_abuse", 1.0, r"\b(execute|executar|rode|rodar|run)\s+(?:\w+\s+){0,2}(comando|command|sql|shell|script|codigo"
     r"|code|python|bash)\b"),
    ("concealment", 0.6, r"\b(nao conte|nao mencione|nao diga|do not tell|don.t tell|do not mention|sem avisar)\b"),
)  # fmt: skip
_COMPILED = tuple((cat, w, re.compile(p, re.IGNORECASE | re.MULTILINE)) for cat, w, p in _RULES)
FLAG_THRESHOLD = 1.0


class ScanResult(BaseModel):
    flagged: bool = False
    score: float = 0.0
    matched_patterns: list[str] = Field(default_factory=list)  # categorias encontradas
    evasion: list[str] = Field(default_factory=list)  # disfarces vistos: invisible, homoglyph, leet, spaced, base64
    out_of_scope_refs: list[str] = Field(default_factory=list)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = _INVISIBLE.sub("", text).translate(_HOMOGLYPHS)
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return text.lower()


def _decoded_base64(text: str) -> list[str]:
    out = []
    for blob in _B64.findall(text):
        try:
            raw = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if raw and sum(c.isprintable() or c.isspace() for c in raw) / len(raw) > 0.9:
            out.append(raw)
    return out


def variants(text: str) -> dict[str, str]:
    """Texto normalizado + as variantes que desfazem cada disfarce (só as que mudam algo)."""
    base = normalize(text)
    found: dict[str, str] = {"plain": base}
    if _INVISIBLE.search(unicodedata.normalize("NFKC", text)):
        found["invisible"] = base
    if unicodedata.normalize("NFKC", text).lower() != unicodedata.normalize("NFKC", text).translate(_HOMOGLYPHS).lower():
        found["homoglyph"] = base
    leet = base.translate(_LEET)
    if leet != base:
        found["leet"] = leet
    despaced = _SPACED.sub(lambda m: re.sub(r"[ .\-_]", "", m.group(0)), base)
    if despaced != base:
        found["spaced"] = despaced
    for i, decoded in enumerate(_decoded_base64(text)):
        found[f"base64#{i}"] = normalize(decoded)
    return found


def _texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [t for v in value.values() for t in _texts(v)]
    if isinstance(value, list):
        return [t for v in value for t in _texts(v)]
    return []


def scan_text(text: str, scope: CaseScope | None = None) -> ScanResult:
    result = ScanResult()
    scope_ids = {c.upper() for c in scope.client_ids} if scope else set()
    weights: dict[str, float] = {}
    for name, variant in variants(text).items():
        hit = False
        for cat, weight, pat in _COMPILED:
            if pat.search(variant):
                hit = True
                weights[cat] = max(weights.get(cat, 0.0), weight)
        for digits in CLIENT_REF_RE.findall(variant):
            ref = "CLIENTE-" + "".join(digits.split())
            if scope is not None and ref not in scope_ids and ref not in result.out_of_scope_refs:
                result.out_of_scope_refs.append(ref)
                hit = True
        kind = name.split("#")[0]
        if hit and kind != "plain" and kind not in result.evasion:
            result.evasion.append(kind)
    result.matched_patterns = sorted(weights)
    # disfarce usado para esconder um padrão é, por si só, sinal: soma meio ponto
    result.score = round(sum(weights.values()) + (0.5 if result.evasion and weights else 0.0), 2)
    result.flagged = result.score >= FLAG_THRESHOLD or bool(result.out_of_scope_refs)
    return result


def scan_record(record: dict[str, Any], scope: CaseScope) -> ScanResult:
    merged = ScanResult()
    for text in _texts({k: v for k, v in record.items() if k != "client_id"}):
        r = scan_text(text, scope)
        merged.score = max(merged.score, r.score)
        for attr in ("matched_patterns", "evasion", "out_of_scope_refs"):
            target = getattr(merged, attr)
            target.extend(x for x in getattr(r, attr) if x not in target)
    merged.matched_patterns.sort()
    merged.flagged = merged.score >= FLAG_THRESHOLD or bool(merged.out_of_scope_refs)
    return merged
