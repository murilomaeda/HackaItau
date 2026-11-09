"""Bootstrap Client Resolver (ARCHITECTURE.md §9.1). Fora do Tool Registry; inacessível a agentes.

Usa SOMENTE a permissão do usuário. Devolve no máximo {client_id, name}. Não lista, não aceita wildcard/prefixo.
Nome parcial só em palavras inteiras e com uma palavra que distinga o cliente; mais de um candidato → `ambiguous`,
sem revelar quais são. Quem decide é sempre o CaseScope que nasce deste resultado.
"""

import re
import unicodedata
from typing import Literal

from pydantic import BaseModel

from app.core.events import EventLog
from app.core.schemas.context import UserIdentity
from app.core.schemas.events import EventType
from app.data.repository import DataRepository

MIN_NAME_LEN = 4
CLIENT_ID_RE = re.compile(r"^CLIENTE-\d{3,}$")
REQUIRED_PERMISSION = "client_profile"
_WILDCARDS = ("*", "%", "?")


class ClientRef(BaseModel):
    client_id: str
    name: str


class ResolveResult(BaseModel):
    status: Literal["ok", "not_found", "ambiguous", "denied"]
    client: ClientRef | None = None
    count: int = 0
    reason: str | None = None

    @classmethod
    def ok(cls, client: ClientRef) -> "ResolveResult":
        return cls(status="ok", client=client, count=1)

    @classmethod
    def not_found(cls) -> "ResolveResult":
        return cls(status="not_found")

    @classmethod
    def ambiguous(cls, count: int) -> "ResolveResult":
        return cls(status="ambiguous", count=count)

    @classmethod
    def denied(cls, reason: str) -> "ResolveResult":
        return cls(status="denied", reason=reason)


def normalize(text: str) -> str:
    """Casefold + remove acentos + colapsa espaços/pontuação leve. Igualdade exata depois disso."""
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_ = "".join(c for c in nfkd if not unicodedata.combining(c))
    cleaned = re.sub(r"[^\w\s\-]", " ", ascii_.casefold())
    return re.sub(r"\s+", " ", cleaned).strip()


_LEGAL_SUFFIXES = (" s a", " sa", " s/a", " ltda", " eireli", " me", " epp")


def legal_core(normalized: str) -> str:
    """Nome normalizado sem o sufixo societário final ("fazenda horizonte s a" → "fazenda horizonte")."""
    core = normalized
    changed = True
    while changed:
        changed = False
        for suf in _LEGAL_SUFFIXES:
            if core.endswith(suf) and len(core) > len(suf):
                core, changed = core[: -len(suf)].rstrip(), True
    return core


# artigos e rótulos antes do nome: o LLM copia o nome como está no texto ("A fazenda horizonte")
_LEADING = {"a", "o", "as", "os", "um", "uma", "da", "do", "de", "das", "dos", "cliente", "empresa", "produtor"}
_ABBREVIATIONS = {
    "faz": "fazenda",
    "fzda": "fazenda",
    "agrop": "agropecuaria",
    "agropec": "agropecuaria",
    "coop": "cooperativa",
}
# palavras que muitos clientes teriam: sozinhas não identificam ninguém
_GENERIC = {
    "fazenda", "fazendas", "agro", "agropecuaria", "agricola", "agroindustrial", "sitio", "cooperativa", "grupo", "usina",
    "cerealista", "empresa", "do", "da", "de", "dos", "das", "e",
}  # fmt: skip
_MIN_DISTINCTIVE_LEN = 4


def name_tokens(ref: str) -> list[str]:
    """Palavras que identificam o nome: sem artigo inicial, sem sufixo societário, abreviações expandidas."""
    tokens = legal_core(normalize(ref)).split()
    while len(tokens) > 1 and tokens[0] in _LEADING:
        tokens = tokens[1:]
    return [_ABBREVIATIONS.get(t, t) for t in tokens]


def matches_partial_name(ref: str, client_name: str) -> bool:
    """Palavras inteiras, todas presentes no nome, e pelo menos uma que distinga o cliente. "Horizonte" casa com
    "Fazenda Horizonte S.A."; "Fazenda" (genérica), "Horiz" (prefixo) e "Horizonte Agro" (palavra a mais) não."""
    wanted = set(name_tokens(ref))
    if not any(t not in _GENERIC and len(t) >= _MIN_DISTINCTIVE_LEN for t in wanted):
        return False
    return wanted <= set(name_tokens(client_name))


def is_client_id(ref: str) -> bool:
    return bool(CLIENT_ID_RE.match(ref.strip().upper()))


class BootstrapClientResolver:
    def __init__(self, repo: DataRepository) -> None:
        self._repo = repo

    def resolve(self, user: UserIdentity, client_ref: str | None, events: EventLog | None = None) -> ResolveResult:
        if REQUIRED_PERMISSION not in user.permissions_read:
            result = ResolveResult.denied("resource_not_authorized_for_user")
            self._audit(events, user, result)
            return result

        raw = (client_ref or "").strip()
        if not raw or any(w in raw for w in _WILDCARDS):
            result = ResolveResult.not_found()
            self._audit(events, user, result)
            return result

        if is_client_id(raw):
            ref = raw.upper()
        else:
            ref = normalize(raw)
            if len(ref) < MIN_NAME_LEN:
                result = ResolveResult.not_found()
                self._audit(events, user, result)
                return result

        matches = self._repo.find_clients_exact(ref)
        if not matches and not is_client_id(raw):
            # o nome inteiro ganha do parcial: "Santa Clara" é ambíguo, "Fazenda Santa Clara" não
            canonical = " ".join(name_tokens(raw))
            matches = self._repo.find_clients_exact(canonical) or self._repo.find_clients_by_partial_name(raw)
        if len(matches) == 0:
            result = ResolveResult.not_found()
        elif len(matches) > 1:
            result = ResolveResult.ambiguous(count=len(matches))
        else:
            m = matches[0]
            result = ResolveResult.ok(ClientRef(client_id=m["client_id"], name=m["name"]))
        self._audit(events, user, result)
        return result

    @staticmethod
    def _audit(events: EventLog | None, user: UserIdentity, result: ResolveResult) -> None:
        if events is None:
            return
        payload = {
            "user_id": user.user_id,
            "matched": result.status == "ok",
            "ambiguous": result.status == "ambiguous",
            "denied": result.status == "denied",
        }
        if result.client:
            payload["client_id"] = result.client.client_id
        events.emit(EventType.BOOTSTRAP_RESOLVED, payload)
