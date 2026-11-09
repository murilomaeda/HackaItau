"""Chave do servidor: assina tokens de sessão e encadeia o audit. Nunca vai para prompt, log ou resposta.

AUTH_SECRET no .env tem prioridade. Sem ele, a chave é gerada uma vez e guardada em state_dir/secret.key (0600), para
que sessões e a verificação do audit sobrevivam a um reinício. Sem persistência, vale só enquanto o processo vive.
"""

import hashlib
import hmac
import os
import secrets
from pathlib import Path

from app.config import Settings

_EPHEMERAL = secrets.token_bytes(32)


def _from_file(path: Path) -> bytes:
    if path.exists():
        return bytes.fromhex(path.read_text(encoding="ascii").strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(key.hex())
    return key


def server_key(s: Settings) -> bytes:
    if s.auth_secret:
        return s.auth_secret.encode()
    if s.persistence:
        return _from_file(s.state_dir / "secret.key")
    return _EPHEMERAL


def derive(key: bytes, purpose: str) -> bytes:
    """Uma subchave por finalidade: quem tem a do audit não forja sessão, e vice-versa."""
    return hmac.new(key, purpose.encode(), hashlib.sha256).digest()
