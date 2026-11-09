"""Autenticação: senha (scrypt), token de sessão (JWT HS256) e limite de tentativas de login.

O usuário de cada requisição vem só do token assinado, nunca do corpo. As permissões continuam vindo de
identities.json (ExecutionContext on-behalf-of). Trocar por OIDC = trocar `issue`/`verify` por validação do IdP.
"""

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass

ISSUER = "atena"
AUDIENCE = "atena-api"
_HEADER = {"alg": "HS256", "typ": "JWT"}

# scrypt (RFC 7914) com custo de memória ~16 MB por tentativa
_N, _R, _P = 2**14, 8, 1


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    """Compara em tempo constante. Sem hash (usuário inexistente) roda o scrypt igual, para não revelar quem existe."""
    try:
        scheme, n, r, p, salt, digest = (stored or _DUMMY_HASH).split("$")
        if scheme != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, _unb64(digest)) and stored is not None


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


_DUMMY_HASH = hash_password("dummy", salt=b"\0" * 16)


@dataclass(frozen=True)
class Session:
    user_id: str
    jti: str
    issued_at: int
    expires_at: int


class TokenError(Exception):
    pass


def issue(key: bytes, user_id: str, ttl_seconds: int, *, now: float | None = None) -> tuple[str, Session]:
    iat = int(now if now is not None else time.time())
    session = Session(user_id=user_id, jti=secrets.token_urlsafe(16), issued_at=iat, expires_at=iat + ttl_seconds)
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": user_id,
        "jti": session.jti,
        "iat": session.issued_at,
        "exp": session.expires_at,
    }
    signing_input = f"{_b64(_json(_HEADER))}.{_b64(_json(claims))}"
    return f"{signing_input}.{_b64(_sign(key, signing_input))}", session


def verify(key: bytes, token: str, *, now: float | None = None) -> Session:
    """Só aceita HS256 com a nossa chave; recusa `alg: none`, outro emissor/audiência, token vencido ou malformado."""
    try:
        header_b64, claims_b64, sig_b64 = token.split(".")
        header = json.loads(_unb64(header_b64))
        claims = json.loads(_unb64(claims_b64))
        signature = _unb64(sig_b64)
    except (ValueError, TypeError) as exc:
        raise TokenError("malformed") from exc
    if header != _HEADER:
        raise TokenError("bad_header")
    if not hmac.compare_digest(signature, _sign(key, f"{header_b64}.{claims_b64}")):
        raise TokenError("bad_signature")
    if claims.get("iss") != ISSUER or claims.get("aud") != AUDIENCE:
        raise TokenError("bad_audience")
    t = now if now is not None else time.time()
    try:
        session = Session(
            user_id=str(claims["sub"]), jti=str(claims["jti"]), issued_at=int(claims["iat"]), expires_at=int(claims["exp"])
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenError("malformed") from exc
    if session.expires_at <= t or session.issued_at > t + 60:
        raise TokenError("expired")
    return session


def _json(value: dict) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def _sign(key: bytes, signing_input: str) -> bytes:
    return hmac.new(key, signing_input.encode(), hashlib.sha256).digest()


class LoginThrottle:
    """Falhas por usuário e por endereço numa janela deslizante. Passou do limite → 429 até a janela andar."""

    def __init__(self, max_failures: int, window_seconds: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.max_failures = max_failures
        self.window = window_seconds
        self._clock = clock
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _recent(self, key: str) -> deque[float]:
        q = self._failures[key]
        cutoff = self._clock() - self.window
        while q and q[0] <= cutoff:
            q.popleft()
        return q

    def retry_after(self, *keys: str) -> int:
        with self._lock:
            waits = [
                int(q[0] + self.window - self._clock()) + 1 for k in keys if len(q := self._recent(k)) >= self.max_failures
            ]
        return max(waits, default=0)

    def failed(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._recent(k).append(self._clock())

    def succeeded(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._failures.pop(k, None)
