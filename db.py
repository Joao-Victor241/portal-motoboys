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
    email                   TEXT,
    foto_path               TEXT,
    bloqueado_permanente    INTEGER NOT NULL DEFAULT 0,
    motivo_bloqueio         TEXT,
    dmp_person_id           INTEGER,
    criado_em               TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- motoboys_ol: vínculo permanente motoboy ↔ OL (sem loja).
-- É o "cadastro". Os campos de trabalho (placa/tipo/valido_ate) ficam aqui.
-- A ativação em uma loja específica fica em `cadastros`.
CREATE TABLE IF NOT EXISTS motoboys_ol (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    motoboy_id  INTEGER NOT NULL REFERENCES motoboys(id),
    ol_id       INTEGER NOT NULL REFERENCES ols(id),
    placa       TEXT,
    tipo        TEXT CHECK (tipo IN ('fixo','free')),
    valido_ate  TEXT,
    criado_por  INTEGER REFERENCES usuarios(id),
    criado_em   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (motoboy_id, ol_id)
);
-- cadastros: ativação de um motoboy em uma loja específica.
CREATE TABLE IF NOT EXISTS cadastros (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    motoboy_id      INTEGER NOT NULL REFERENCES motoboys(id),
    ol_id           INTEGER NOT NULL REFERENCES ols(id),
    loja_id         INTEGER NOT NULL REFERENCES lojas(id),
    situacao        TEXT NOT NULL DEFAULT 'ativo',
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
-- Eventos de entrada/saída lidos do DMP (alimenta a fila do operador).
CREATE TABLE IF NOT EXISTS acesso_eventos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    motoboy_id  INTEGER REFERENCES motoboys(id),
    loja_id     INTEGER REFERENCES lojas(id),
    cpf         TEXT,
    nome        TEXT,
    tipo        TEXT,                     -- entrada / saida
    ocorrido_em TEXT NOT NULL,
    dmp_pointer INTEGER
);
-- Controle do último ponteiro de AccessLog já lido do DMP (1 linha).
CREATE TABLE IF NOT EXISTS acesso_estado (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    ultimo_pointer INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO acesso_estado (id, ultimo_pointer) VALUES (1, 0);
"""


def _hash(senha: str) -> str:
    return bcrypt.hashpw(senha.encode(), bcrypt.gensalt()).decode()


# Senhas dos usuários padrão vêm dos Secrets/.env (nunca ficam no código).
# Se a variável não estiver definida, cai no fallback (mantém funcionando).
_MAPA_SENHA_ENV = {
    "admin":      ("ADMIN_PASSWORD", "admin123"),
    "ol_exemplo": ("OL_EXEMPLO_PASSWORD", "ol123"),
    "operador":   ("OPERADOR_PASSWORD", "op123"),
}
_SENHAS_SINCRONIZADAS = False


def _senha_padrao(login: str) -> str:
    chave, fallback = _MAPA_SENHA_ENV[login]
    return os.getenv(chave, fallback)


# Nomes oficiais das lojas Kaizen (chave = código, valor = nome).
LOJAS_KAIZEN = {
    "L01": "Kaizen - Asa Norte",
    "L02": "Kaizen - Ceilândia",
    "L03": "Kaizen - Gama",
    "L04": "Kaizen - SOF Sul",
    "L05": "Kaizen - Planaltina",
}


def inicializar():
    """Cria as tabelas, aplica migrações e semeia dados de exemplo na 1ª execução."""
    conn = conectar()
    conn.executescript(ESQUEMA)

    # Migração: adiciona colunas novas em bancos já existentes (idempotente).
    for coluna in ("email TEXT", "telefone TEXT"):
        try:
            conn.execute(f"ALTER TABLE motoboys ADD COLUMN {coluna}")
        except Exception:
            pass

    # Migração: popula motoboys_ol a partir de cadastros antigos (uma vez).
    # Cadastros antigos tinham placa/tipo/valido_ate que agora ficam em motoboys_ol.
    has_mol = conn.execute("SELECT COUNT(*) FROM motoboys_ol").fetchone()[0]
    if has_mol == 0:
        try:
            old_cols = [r[1] for r in conn.execute("PRAGMA table_info(cadastros)").fetchall()]
            if "placa" in old_cols:
                conn.execute("""
                    INSERT OR IGNORE INTO motoboys_ol (motoboy_id, ol_id, placa, tipo, valido_ate, criado_por)
                    SELECT motoboy_id, ol_id, placa, tipo, valido_ate, criado_por
                    FROM cadastros
                """)
        except Exception:
            pass

    # Remove colunas antigas de cadastros se ainda existirem (recriar tabela).
    old_cols = [r[1] for r in conn.execute("PRAGMA table_info(cadastros)").fetchall()]
    if "placa" in old_cols or "tipo" in old_cols or "valido_ate" in old_cols:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS cadastros_v2 (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                motoboy_id  INTEGER NOT NULL REFERENCES motoboys(id),
                ol_id       INTEGER NOT NULL REFERENCES ols(id),
                loja_id     INTEGER NOT NULL REFERENCES lojas(id),
                situacao    TEXT NOT NULL DEFAULT 'ativo',
                enviado_siac INTEGER NOT NULL DEFAULT 0,
                enviado_dmp  INTEGER NOT NULL DEFAULT 0,
                criado_por  INTEGER REFERENCES usuarios(id),
                criado_em   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (motoboy_id, ol_id, loja_id)
            );
            INSERT OR IGNORE INTO cadastros_v2
                (id, motoboy_id, ol_id, loja_id, situacao, enviado_siac, enviado_dmp, criado_por, criado_em)
            SELECT id, motoboy_id, ol_id, loja_id, situacao, enviado_siac, enviado_dmp, criado_por, criado_em
            FROM cadastros;
            DROP TABLE cadastros;
            ALTER TABLE cadastros_v2 RENAME TO cadastros;
        """)

    # Atualiza nomes das lojas para os nomes oficiais Kaizen (idempotente).
    for codigo, nome in LOJAS_KAIZEN.items():
        conn.execute("UPDATE lojas SET nome=? WHERE codigo=?", (nome, codigo))

    # Só semeia se ainda não há usuários (primeira execução).
    if conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] == 0:
        for codigo, nome in LOJAS_KAIZEN.items():
            conn.execute("INSERT INTO lojas (codigo, nome) VALUES (?, ?)", (codigo, nome))
        cur = conn.execute(
            "INSERT INTO ols (nome, cnpj, limite_global) VALUES (?, ?, ?)",
            ("OL Exemplo Transportes", "00000000000191", 50))
        ol_id = cur.lastrowid
        for loja in conn.execute("SELECT id FROM lojas").fetchall():
            conn.execute("INSERT INTO ol_loja_limite (ol_id, loja_id, limite) VALUES (?,?,?)",
                         (ol_id, loja["id"], 10))
        conn.execute("INSERT INTO usuarios (login, senha_hash, perfil) VALUES (?,?,?)",
                     ("admin", _hash(_senha_padrao("admin")), "admin"))
        conn.execute("INSERT INTO usuarios (login, senha_hash, perfil, ol_id) VALUES (?,?,?,?)",
                     ("ol_exemplo", _hash(_senha_padrao("ol_exemplo")), "ol", ol_id))
        conn.execute("INSERT INTO usuarios (login, senha_hash, perfil) VALUES (?,?,?)",
                     ("operador", _hash(_senha_padrao("operador")), "operador"))

    # Sincroniza as senhas dos usuários padrão com os Secrets (1x por processo).
    # Só atualiza quando a variável de ambiente estiver definida — assim, definir
    # ADMIN_PASSWORD/etc. nos Secrets passa a valer mesmo se o banco já existia.
    global _SENHAS_SINCRONIZADAS
    if not _SENHAS_SINCRONIZADAS:
        for login, (chave, _fb) in _MAPA_SENHA_ENV.items():
            if os.getenv(chave):
                conn.execute("UPDATE usuarios SET senha_hash=? WHERE login=?",
                             (_hash(_senha_padrao(login)), login))
        _SENHAS_SINCRONIZADAS = True

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
