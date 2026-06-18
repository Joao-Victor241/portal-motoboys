"""Autenticação de usuários do portal (login + senha com bcrypt)."""

import bcrypt
from db import conectar


def autenticar(login: str, senha: str) -> dict | None:
    """Devolve os dados do usuário se login/senha conferirem; senão, None."""
    conn = conectar()
    u = conn.execute(
        "SELECT * FROM usuarios WHERE login = ? AND ativo = 1", (login.strip(),)
    ).fetchone()
    conn.close()
    if u and bcrypt.checkpw(senha.encode(), u["senha_hash"].encode()):
        return {"id": u["id"], "login": u["login"], "perfil": u["perfil"], "ol_id": u["ol_id"]}
    return None
