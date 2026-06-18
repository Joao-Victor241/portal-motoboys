"""
Camada de banco de dados do portal.

Nesta Fase 1 usamos SQLite local (arquivo `portal.db`) para o portal rodar na
mão sem depender de servidor. Em produção o alvo é o PostgreSQL de `db/schema.sql`.
Por isso o código de acesso fica concentrado aqui — trocar para Postgres depois
mexe só neste arquivo.
"""

import sqlite3
import os
import bcrypt

CAMINHO_BANCO = os.path.join(os.path.dirname(__file__), "portal.db")


def conectar() -> sqlite3.Connection:
    conn = sqlite3.connect(CAMINHO_BANCO)
    conn.row_factory = sqlite3.Row          # acessar colunas pelo nome
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# --- Esquema (espelha db/schema.sql, em dialeto SQLite) --------------------

ESQUEMA = """
CREATE TABLE IF NOT EXISTS lojas (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    codigo  TEXT UNIQUE NOT NULL,
    nome    TEXT NOT NULL,
    ativo   INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS ols (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    nome            TEXT NOT NULL,
    cnpj            TEXT UNIQUE,
    ativo           INTEGER NOT NULL DEFAULT 1,
    limite_global   INTEGER
);
CREATE TABLE IF NOT EXISTS ol_loja_limite (
    ol_id   INTEGER NOT NULL REFERENCES ols(id),
    loja_id INTEGER NOT NULL REFERENCES lojas(id),
    limite  INTEGER NOT NULL,
    PRIMARY KEY (ol_id, loja_id)
);
CREATE TABLE IF NOT EXISTS usuarios (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    login       TEXT UNIQUE NOT NULL,
    senha_hash  TEXT NOT NULL,
    perfil      TEXT NOT NULL CHECK (perfil IN ('admin','ol','operador')),
    ol_id       INTEGER REFERENCES ols(id),
    ativo       INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS motoboys (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    cpf                     TEXT UNIQUE NOT NULL,
    nome                    TEXT NOT NULL,
    nascimento              TEXT,
    cnh                     TEXT,
    cnh_venc                TEXT,
    telefone                TEXT,
    foto_path               TEXT,
    bloqueado_permanente    INTEGER NOT NULL DEFAULT 0,
    motivo_bloqueio         TEXT,
    dmp_person_id           INTEGER,
    criado_em               TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS cadastros (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    motoboy_id      INTEGER NOT NULL REFERENCES motoboys(id),
    ol_id           INTEGER NOT NULL REFERENCES ols(id),
    loja_id         INTEGER NOT NULL REFERENCES lojas(id),
    placa           TEXT,
    tipo            TEXT CHECK (tipo IN ('fixo','free')),
    situacao        TEXT NOT NULL DEFAULT 'ativo',
    valido_de       TEXT,
    valido_ate      TEXT,
    enviado_siac    INTEGER NOT NULL DEFAULT 0,
    enviado_dmp     INTEGER NOT NULL DEFAULT 0,
    criado_por      INTEGER REFERENCES usuarios(id),
    criado_em       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (motoboy_id, ol_id, loja_id)
);
CREATE TABLE IF NOT EXISTS selfie_links (
    token       TEXT PRIMARY KEY,
    motoboy_id  INTEGER NOT NULL REFERENCES motoboys(id),
    expira_em   TEXT NOT NULL,
    usado_em    TEXT
);
CREATE TABLE IF NOT EXISTS auditoria (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id  INTEGER REFERENCES usuarios(id),
    acao        TEXT NOT NULL,
    entidade    TEXT,
    entidade_id INTEGER,
    detalhe     TEXT,
    criado_em   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def _hash(senha: str) -> str:
    return bcrypt.hashpw(senha.encode(), bcrypt.gensalt()).decode()


def inicializar():
    """Cria as tabelas e, se o banco estiver vazio, semeia dados de exemplo."""
    conn = conectar()
    conn.executescript(ESQUEMA)

    # Só semeia se ainda não há usuários (primeira execução).
    if conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] == 0:
        # 5 lojas da operação
        for i in range(1, 6):
            conn.execute("INSERT INTO lojas (codigo, nome) VALUES (?, ?)",
                         (f"L{i:02d}", f"Loja {i:02d}"))
        # Uma OL de exemplo
        cur = conn.execute(
            "INSERT INTO ols (nome, cnpj, limite_global) VALUES (?, ?, ?)",
            ("OL Exemplo Transportes", "00000000000191", 50))
        ol_id = cur.lastrowid
        # Limite de 10 motoboys dessa OL em cada loja
        for loja in conn.execute("SELECT id FROM lojas").fetchall():
            conn.execute("INSERT INTO ol_loja_limite (ol_id, loja_id, limite) VALUES (?,?,?)",
                         (ol_id, loja["id"], 10))
        # Usuários: um por perfil. (Senhas de exemplo — trocar em produção.)
        conn.execute("INSERT INTO usuarios (login, senha_hash, perfil) VALUES (?,?,?)",
                     ("admin", _hash("admin123"), "admin"))
        conn.execute("INSERT INTO usuarios (login, senha_hash, perfil, ol_id) VALUES (?,?,?,?)",
                     ("ol_exemplo", _hash("ol123"), "ol", ol_id))
        conn.execute("INSERT INTO usuarios (login, senha_hash, perfil) VALUES (?,?,?)",
                     ("operador", _hash("op123"), "operador"))
    conn.commit()
    conn.close()


def criar_ol(conn, nome, cnpj, limite_global):
    """Cria uma OL e já abre os limites (0) dela em cada loja. Devolve o id."""
    cur = conn.execute("INSERT INTO ols (nome, cnpj, limite_global) VALUES (?,?,?)",
                       (nome, cnpj or None, limite_global))
    ol_id = cur.lastrowid
    for loja in conn.execute("SELECT id FROM lojas").fetchall():
        conn.execute("INSERT INTO ol_loja_limite (ol_id, loja_id, limite) VALUES (?,?,?)",
                     (ol_id, loja["id"], 0))
    return ol_id


def criar_usuario(conn, login, senha, perfil, ol_id=None):
    """Cria um usuário do portal com a senha já criptografada (bcrypt)."""
    conn.execute("INSERT INTO usuarios (login, senha_hash, perfil, ol_id) VALUES (?,?,?,?)",
                 (login.strip(), _hash(senha), perfil, ol_id))


def auditar(conn, usuario_id, acao, entidade=None, entidade_id=None, detalhe=None):
    conn.execute(
        "INSERT INTO auditoria (usuario_id, acao, entidade, entidade_id, detalhe) "
        "VALUES (?,?,?,?,?)",
        (usuario_id, acao, entidade, entidade_id, detalhe))
