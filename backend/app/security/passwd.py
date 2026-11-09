"""Gera o password_hash de um usuário para identities.json: python -m app.security.passwd (pede a senha sem eco)."""

import getpass

from app.security.auth import hash_password

if __name__ == "__main__":
    first = getpass.getpass("Senha: ")
    if len(first) < 12 or first != getpass.getpass("Repita: "):
        raise SystemExit("Senhas diferentes ou com menos de 12 caracteres.")
    print(hash_password(first))
