"""
Camada de banco de dados do portal.

Suporta DOIS bancos, escolhidos em tempo de execução:
  - PostgreSQL: quando a variável de ambiente DATABASE_URL está definida
    (produção — dados persistentes; é o caso do banco da empresa/TI).
  - SQLite local (arquivo `portal.db`): fallback de desenvolvimento.

O resto do código (app.py, regras.py) usa `conn.execute("... ?", (...))` como
sempre — aqui há um adaptador que traduz para o PostgreSQL quando necessário,
então não é preciso mexer nas dezenas de queries espalhadas pelo app.

Variáveis de ambiente (Secrets do Streamlit):
  DATABASE_URL  ex.: postgresql://usuario:senha@host:5432/banco?sslmode=require
  DB_SCHEMA     schema onde ficam as tabelas (padrão: motoboys_portal)
"""

import sqlite3
import os
import bcrypt

CAMINHO_BANCO = os.path.join(os.path.dirname(__file__), "portal.db")


def usando_pg() -> bool:
    """True se houver DATABASE_URL (modo PostgreSQL). Lido a cada chamada porque
    os Secrets do Streamlit só entram no os.environ depois do import deste módulo."""
    return bool(os.getenv("DATABASE_URL", "").strip())


def _schema_pg() -> str:
    return (os.getenv("DB_SCHEMA", "").strip() or "motoboys_portal")


# ---------------------------------------------------------------------------
# Adaptador PostgreSQL — faz o psycopg "parecer" com o sqlite3 para o app:
#   - traduz placeholders ? -> %s
#   - linhas acessíveis por nome (row['col']) E por índice (row[0])
#   - .execute(...).fetchone()/.fetchall(), .commit(), .rollback(), .close()
# ---------------------------------------------------------------------------

def _traduzir_sql(sql: str, tem_params: bool) -> str:
    # No PostgreSQL/psycopg o placeholder é %s; aqui o código usa ?.
    # Quando há params, % literais precisam virar %% (não temos % nas queries,
    # mas escapamos por segurança).
    if tem_params:
        sql = sql.replace("%", "%%")
    return sql.replace("?", "%s")


class _LinhaPG:
    """Imita sqlite3.Row: aceita row['coluna'] e row[0]."""
    __slots__ = ("_vals", "_map")

    def __init__(self, cols, vals):
        self._vals = vals
        self._map = {c: v for c, v in zip(cols, vals)}

    def __getitem__(self, chave):
        if isinstance(chave, int):
            return self._vals[chave]
        return self._map[chave]

    def get(self, chave, padrao=None):
        return self._map.get(chave, padrao)

    def keys(self):
        return list(self._map.keys())

    def __iter__(self):
        return iter(self._vals)


class _CursorPG:
    def __init__(self, cur):
        self._cur = cur

    def _cols(self):
        return [d[0] for d in self._cur.description] if self._cur.description else []

    def fetchone(self):
        r = self._cur.fetchone()
        return _LinhaPG(self._cols(), r) if r is not None else None

    def fetchall(self):
        cols = self._cols()
        return [_LinhaPG(cols, r) for r in self._cur.fetchall()]

    def __iter__(self):
        cols = self._cols()
        for r in self._cur:
            yield _LinhaPG(cols, r)


class _ConexaoPG:
    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        cur = self._raw.cursor()
        cur.execute(_traduzir_sql(sql, bool(params)), params if params else None)
        return _CursorPG(cur)

    def executescript(self, script):
        cur = self._raw.cursor()
        for stmt in script.split(";"):
            s = stmt.strip()
            if s:
                cur.execute(s)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()


_SCHEMA_CRIADO = False


def conectar():
    if usando_pg():
        import psycopg
        global _SCHEMA_CRIADO
        raw = psycopg.connect(os.getenv("DATABASE_URL").strip(), autocommit=True)
        schema = _schema_pg()
        cur = raw.cursor()
        # CREATE SCHEMA só uma vez por processo (evita um round-trip a cada conexão).
        if not _SCHEMA_CRIADO:
            try:
                cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            except Exception:
                pass
            _SCHEMA_CRIADO = True
        try:
            cur.execute(f'SET search_path TO "{schema}"')
        except Exception:
            pass
        cur.close()
        raw.autocommit = False   # o app controla commit/rollback
        return _ConexaoPG(raw)

    # --- SQLite (desenvolvimento) ---
    # timeout=15: espera até 15s por um lock em vez de falhar na hora
    # (o Streamlit abre várias conexões concorrentes ao mesmo arquivo).
    conn = sqlite3.connect(CAMINHO_BANCO, timeout=15)
    conn.row_factory = sqlite3.Row          # acessar colunas pelo nome
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    # WAL: permite leitura concorrente com escrita — reduz "database is locked".
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except Exception:
        pass
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
    perfil      TEXT NOT NULL,
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
    treinamento_em          TEXT,
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
-- Prestação de contas: documentos (recibos, guias) enviados pela OL.
-- O arquivo é guardado no próprio banco (BLOB) para persistir e ser lido depois
-- pela extensão de leitura automática.
CREATE TABLE IF NOT EXISTS prestacao_documentos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ol_id        INTEGER NOT NULL REFERENCES ols(id),
    tipo         TEXT NOT NULL,
    competencia  TEXT,                                  -- 'AAAA-MM'
    escopo       TEXT NOT NULL DEFAULT 'individual',    -- individual | geral
    nome_arquivo TEXT,
    mime         TEXT,
    arquivo      BLOB,
    status       TEXT NOT NULL DEFAULT 'pendente',      -- pendente | validado | rejeitado
    criado_por   INTEGER REFERENCES usuarios(id),
    criado_em    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- Valor de cada documento. Por motoboy (individual/geral) e/ou por tipo
-- (quando o arquivo é "Outros" e contém vários documentos juntos).
CREATE TABLE IF NOT EXISTS prestacao_valores (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    documento_id INTEGER NOT NULL REFERENCES prestacao_documentos(id),
    motoboy_id   INTEGER REFERENCES motoboys(id),
    tipo         TEXT,
    valor        REAL
);
-- Arquivos de um documento de prestação (permite vários por envio).
CREATE TABLE IF NOT EXISTS prestacao_arquivos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    documento_id INTEGER NOT NULL REFERENCES prestacao_documentos(id),
    nome_arquivo TEXT,
    mime         TEXT,
    arquivo      BLOB
);
-- Configurações gerais do portal (chave/valor). Ex.: prazo de prestação.
CREATE TABLE IF NOT EXISTS configuracoes (
    chave TEXT PRIMARY KEY,
    valor TEXT
);
"""


# --- Mesmo esquema, em dialeto PostgreSQL -----------------------------------
# Diferenças vs SQLite: SERIAL no lugar de INTEGER AUTOINCREMENT; criado_em como
# TEXT no formato 'AAAA-MM-DD HH:MM:SS' (para o app continuar tratando como
# string, igual ao SQLite); ON CONFLICT DO NOTHING no lugar de INSERT OR IGNORE.
_TS = "to_char(now(), 'YYYY-MM-DD HH24:MI:SS')"
ESQUEMA_PG = f"""
CREATE TABLE IF NOT EXISTS lojas (
    id      SERIAL PRIMARY KEY,
    codigo  TEXT UNIQUE NOT NULL,
    nome    TEXT NOT NULL,
    ativo   INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS ols (
    id              SERIAL PRIMARY KEY,
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
    id          SERIAL PRIMARY KEY,
    login       TEXT UNIQUE NOT NULL,
    senha_hash  TEXT NOT NULL,
    perfil      TEXT NOT NULL,
    ol_id       INTEGER REFERENCES ols(id),
    ativo       INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS motoboys (
    id                      SERIAL PRIMARY KEY,
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
    dmp_person_id           BIGINT,
    treinamento_em          TEXT,
    criado_em               TEXT NOT NULL DEFAULT {_TS}
);
CREATE TABLE IF NOT EXISTS motoboys_ol (
    id          SERIAL PRIMARY KEY,
    motoboy_id  INTEGER NOT NULL REFERENCES motoboys(id),
    ol_id       INTEGER NOT NULL REFERENCES ols(id),
    placa       TEXT,
    tipo        TEXT CHECK (tipo IN ('fixo','free')),
    valido_ate  TEXT,
    criado_por  INTEGER REFERENCES usuarios(id),
    criado_em   TEXT NOT NULL DEFAULT {_TS},
    UNIQUE (motoboy_id, ol_id)
);
CREATE TABLE IF NOT EXISTS cadastros (
    id              SERIAL PRIMARY KEY,
    motoboy_id      INTEGER NOT NULL REFERENCES motoboys(id),
    ol_id           INTEGER NOT NULL REFERENCES ols(id),
    loja_id         INTEGER NOT NULL REFERENCES lojas(id),
    situacao        TEXT NOT NULL DEFAULT 'ativo',
    enviado_siac    INTEGER NOT NULL DEFAULT 0,
    enviado_dmp     INTEGER NOT NULL DEFAULT 0,
    criado_por      INTEGER REFERENCES usuarios(id),
    criado_em       TEXT NOT NULL DEFAULT {_TS},
    UNIQUE (motoboy_id, ol_id, loja_id)
);
CREATE TABLE IF NOT EXISTS selfie_links (
    token       TEXT PRIMARY KEY,
    motoboy_id  INTEGER NOT NULL REFERENCES motoboys(id),
    expira_em   TEXT NOT NULL,
    usado_em    TEXT
);
CREATE TABLE IF NOT EXISTS auditoria (
    id          BIGSERIAL PRIMARY KEY,
    usuario_id  INTEGER REFERENCES usuarios(id),
    acao        TEXT NOT NULL,
    entidade    TEXT,
    entidade_id INTEGER,
    detalhe     TEXT,
    criado_em   TEXT NOT NULL DEFAULT {_TS}
);
CREATE TABLE IF NOT EXISTS acesso_eventos (
    id          BIGSERIAL PRIMARY KEY,
    motoboy_id  INTEGER REFERENCES motoboys(id),
    loja_id     INTEGER REFERENCES lojas(id),
    cpf         TEXT,
    nome        TEXT,
    tipo        TEXT,
    ocorrido_em TEXT NOT NULL,
    dmp_pointer BIGINT
);
CREATE TABLE IF NOT EXISTS acesso_estado (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    ultimo_pointer BIGINT NOT NULL DEFAULT 0
);
INSERT INTO acesso_estado (id, ultimo_pointer) VALUES (1, 0) ON CONFLICT (id) DO NOTHING;
CREATE TABLE IF NOT EXISTS prestacao_documentos (
    id           SERIAL PRIMARY KEY,
    ol_id        INTEGER NOT NULL REFERENCES ols(id),
    tipo         TEXT NOT NULL,
    competencia  TEXT,
    escopo       TEXT NOT NULL DEFAULT 'individual',
    nome_arquivo TEXT,
    mime         TEXT,
    arquivo      BYTEA,
    status       TEXT NOT NULL DEFAULT 'pendente',
    criado_por   INTEGER REFERENCES usuarios(id),
    criado_em    TEXT NOT NULL DEFAULT {_TS}
);
CREATE TABLE IF NOT EXISTS prestacao_valores (
    id           SERIAL PRIMARY KEY,
    documento_id INTEGER NOT NULL REFERENCES prestacao_documentos(id),
    motoboy_id   INTEGER REFERENCES motoboys(id),
    tipo         TEXT,
    valor        NUMERIC(12,2)
);
CREATE TABLE IF NOT EXISTS prestacao_arquivos (
    id           SERIAL PRIMARY KEY,
    documento_id INTEGER NOT NULL REFERENCES prestacao_documentos(id),
    nome_arquivo TEXT,
    mime         TEXT,
    arquivo      BYTEA
);
CREATE TABLE IF NOT EXISTS configuracoes (
    chave TEXT PRIMARY KEY,
    valor TEXT
);
"""


def _hash(senha: str) -> str:
    return bcrypt.hashpw(senha.encode(), bcrypt.gensalt()).decode()


# Senhas dos usuários padrão vêm dos Secrets/.env (nunca ficam no código).
# Se a variável não estiver definida, cai no fallback (mantém funcionando).
_MAPA_SENHA_ENV = {
    "admin":       ("ADMIN_PASSWORD", "admin123"),
    "ol_exemplo":  ("OL_EXEMPLO_PASSWORD", "ol123"),
    "operador":    ("OPERADOR_PASSWORD", "op123"),
    "conferencia": ("CONFERENCIA_PASSWORD", "conf123"),  # conferência de documentos
    "financeiro":  ("FINANCEIRO_PASSWORD", "fin123"),    # faturas/boletos/NF
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


# Guarda qual backend já foi inicializado nesta execução ('pg' ou 'sqlite').
# Por backend (não só True/False) para reinicializar se trocar de banco em runtime.
_BACKEND_INICIALIZADO = None


def inicializar():
    """Cria as tabelas, aplica migrações e semeia dados de exemplo.
    Roda só UMA vez por processo (não a cada rerun do Streamlit) — evita
    contenção/lock no SQLite e custo desnecessário."""
    global _BACKEND_INICIALIZADO, _SENHAS_SINCRONIZADAS
    backend = "pg" if usando_pg() else "sqlite"
    if _BACKEND_INICIALIZADO == backend:
        return

    # ---- Caminho PostgreSQL (produção) ----
    if usando_pg():
        conn = conectar()
        conn.executescript(ESQUEMA_PG)
        # Nomes oficiais das lojas (idempotente).
        for codigo, nome in LOJAS_KAIZEN.items():
            conn.execute("UPDATE lojas SET nome=? WHERE codigo=?", (nome, codigo))
        # Correção pontual de nome (one-off).
        conn.execute("UPDATE motoboys SET nome='Ludmilla' "
                     "WHERE cpf='71470514400' AND nome='Teste free 1'")
        # Semeia na primeira execução.
        if conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] == 0:
            for codigo, nome in LOJAS_KAIZEN.items():
                conn.execute("INSERT INTO lojas (codigo, nome) VALUES (?, ?)", (codigo, nome))
            ol_id = conn.execute(
                "INSERT INTO ols (nome, cnpj, limite_global) VALUES (?, ?, ?) RETURNING id",
                ("OL Exemplo Transportes", "00000000000191", 50)).fetchone()[0]
            for loja in conn.execute("SELECT id FROM lojas").fetchall():
                conn.execute("INSERT INTO ol_loja_limite (ol_id, loja_id, limite) VALUES (?,?,?)",
                             (ol_id, loja["id"], 10))
            conn.execute("INSERT INTO usuarios (login, senha_hash, perfil) VALUES (?,?,?)",
                         ("admin", _hash(_senha_padrao("admin")), "admin"))
            conn.execute("INSERT INTO usuarios (login, senha_hash, perfil, ol_id) VALUES (?,?,?,?)",
                         ("ol_exemplo", _hash(_senha_padrao("ol_exemplo")), "ol", ol_id))
            conn.execute("INSERT INTO usuarios (login, senha_hash, perfil) VALUES (?,?,?)",
                         ("operador", _hash(_senha_padrao("operador")), "operador"))
        # Garante o perfil financeiro (novo) mesmo em bancos já existentes.
        # Remove QUALQUER CHECK de perfil (o nome gerado pode variar) buscando o
        # nome real no catálogo — assim o INSERT abaixo nunca esbarra no CHECK antigo.
        try:
            cons = conn.execute(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid='usuarios'::regclass AND contype='c'").fetchall()
            for cc in cons:
                conn.execute(f'ALTER TABLE usuarios DROP CONSTRAINT IF EXISTS "{cc[0]}"')
        except Exception:
            pass
        if conn.execute("SELECT COUNT(*) FROM usuarios WHERE login='financeiro'").fetchone()[0] == 0:
            conn.execute("INSERT INTO usuarios (login, senha_hash, perfil) VALUES (?,?,?)",
                         ("financeiro", _hash(_senha_padrao("financeiro")), "financeiro"))
        # Perfil de conferência de documentos (novo nome do antigo "financeiro").
        if conn.execute("SELECT COUNT(*) FROM usuarios WHERE login='conferencia'").fetchone()[0] == 0:
            conn.execute("INSERT INTO usuarios (login, senha_hash, perfil) VALUES (?,?,?)",
                         ("conferencia", _hash(_senha_padrao("conferencia")), "conferencia"))
        # Sincroniza senhas com os Secrets (1x por processo).
        if not _SENHAS_SINCRONIZADAS:
            for login, (chave, _fb) in _MAPA_SENHA_ENV.items():
                if os.getenv(chave):
                    conn.execute("UPDATE usuarios SET senha_hash=? WHERE login=?",
                                 (_hash(_senha_padrao(login)), login))
            _SENHAS_SINCRONIZADAS = True
        _garantir_treinamento(conn)
        conn.commit()
        conn.close()
        _BACKEND_INICIALIZADO = backend
        return

    # ---- Caminho SQLite (desenvolvimento) ----
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

    # Correção pontual de nome (one-off, idempotente: só renomeia se ainda
    # estiver com o nome antigo). Pedido manual — sem expor edição de nome na UI.
    conn.execute(
        "UPDATE motoboys SET nome='Ludmilla' WHERE cpf='71470514400' AND nome='Teste free 1'")

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

    # Garante o perfil financeiro (novo) mesmo em bancos já existentes.
    if conn.execute("SELECT COUNT(*) FROM usuarios WHERE login='financeiro'").fetchone()[0] == 0:
        try:
            conn.execute("INSERT INTO usuarios (login, senha_hash, perfil) VALUES (?,?,?)",
                         ("financeiro", _hash(_senha_padrao("financeiro")), "financeiro"))
        except Exception:
            pass
    # Perfil de conferência de documentos (antigo "financeiro").
    if conn.execute("SELECT COUNT(*) FROM usuarios WHERE login='conferencia'").fetchone()[0] == 0:
        try:
            conn.execute("INSERT INTO usuarios (login, senha_hash, perfil) VALUES (?,?,?)",
                         ("conferencia", _hash(_senha_padrao("conferencia")), "conferencia"))
        except Exception:
            pass

    # Sincroniza as senhas dos usuários padrão com os Secrets (1x por processo).
    # Só atualiza quando a variável de ambiente estiver definida — assim, definir
    # ADMIN_PASSWORD/etc. nos Secrets passa a valer mesmo se o banco já existia.
    if not _SENHAS_SINCRONIZADAS:
        for login, (chave, _fb) in _MAPA_SENHA_ENV.items():
            if os.getenv(chave):
                conn.execute("UPDATE usuarios SET senha_hash=? WHERE login=?",
                             (_hash(_senha_padrao(login)), login))
        _SENHAS_SINCRONIZADAS = True

    _garantir_treinamento(conn)
    conn.commit()
    conn.close()
    _BACKEND_INICIALIZADO = backend


def criar_ol(conn, nome, cnpj, limite_global):
    """Cria uma OL e já abre os limites (0) dela em cada loja. Devolve o id."""
    if usando_pg():
        ol_id = conn.execute(
            "INSERT INTO ols (nome, cnpj, limite_global) VALUES (?,?,?) RETURNING id",
            (nome, cnpj or None, limite_global)).fetchone()[0]
    else:
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


def _inserir_id(conn, sql, params):
    """Executa um INSERT e devolve o id da linha criada (funciona em PG e SQLite)."""
    if usando_pg():
        return conn.execute(sql + " RETURNING id", params).fetchone()[0]
    return conn.execute(sql, params).lastrowid


def _garantir_treinamento(conn):
    """Cria tabelas de runtime (vídeo de treinamento, fila de expedição) e a
    coluna motoboys.treinamento_em (idempotente). Chamado no inicializar, nos 2 bancos."""
    pg = usando_pg()
    serial = "SERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    blob = "BYTEA" if pg else "BLOB"
    ts = "to_char(now(), 'YYYY-MM-DD HH24:MI:SS')" if pg else "CURRENT_TIMESTAMP"
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS treinamento_video ("
        f" id {serial}, nome_arquivo TEXT, mime TEXT, dados {blob},"
        f" criado_em TEXT NOT NULL DEFAULT {ts})")
    # Fila FIFO de expedição do operador (chegada do motoboy à expedição).
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS fila_expedicao ("
        f" id {serial}, loja_id INTEGER NOT NULL REFERENCES lojas(id),"
        f" motoboy_id INTEGER NOT NULL REFERENCES motoboys(id),"
        f" chegada_em TEXT NOT NULL, situacao TEXT NOT NULL DEFAULT 'aguardando',"
        f" despachado_em TEXT)")
    if pg:
        conn.execute("ALTER TABLE motoboys ADD COLUMN IF NOT EXISTS treinamento_em TEXT")
        conn.execute(f"ALTER TABLE motoboys ADD COLUMN IF NOT EXISTS foto_selfie {blob}")
    else:
        for _c in ("treinamento_em TEXT", f"foto_selfie {blob}"):
            try:
                conn.execute(f"ALTER TABLE motoboys ADD COLUMN {_c}")
            except Exception:
                pass


def salvar_foto_selfie(conn, motoboy_id, foto_bytes):
    """Guarda a selfie no banco (para reenviar ao DMP junto com a situação na ativação)."""
    conn.execute("UPDATE motoboys SET foto_selfie=? WHERE id=?", (foto_bytes, motoboy_id))
    conn.commit()


def get_foto_selfie(conn, motoboy_id):
    """Bytes da selfie guardada (ou None)."""
    r = conn.execute("SELECT foto_selfie FROM motoboys WHERE id=?", (motoboy_id,)).fetchone()
    if r and r[0] is not None:
        return bytes(r[0])
    return None


# ---- Senha de acesso do operador por loja (guardada em configuracoes) -----

def set_senha_loja(conn, loja_id, senha):
    """Define/atualiza a senha que o operador usa para acessar esta loja (bcrypt)."""
    set_config(conn, f"op_senha_{loja_id}", _hash(senha))
    conn.commit()


def tem_senha_loja(conn, loja_id) -> bool:
    return get_config(conn, f"op_senha_{loja_id}") is not None


def verificar_senha_loja(conn, loja_id, senha) -> bool:
    h = get_config(conn, f"op_senha_{loja_id}")
    if not h:
        return False
    try:
        return bcrypt.checkpw((senha or "").encode(), h.encode())
    except Exception:
        return False


def remover_senha_loja(conn, loja_id):
    conn.execute("DELETE FROM configuracoes WHERE chave=?", (f"op_senha_{loja_id}",))
    conn.commit()


# ---- Mapa catraca/equipamento (EquipmentNumber do DMP) -> loja -------------

def set_equip_loja(conn, loja_id, equipamentos):
    """Define quais catracas (EquipmentNumber) pertencem a esta loja.
    Aceita vários separados por vírgula."""
    set_config(conn, f"equip_loja_{loja_id}", (equipamentos or "").strip())
    conn.commit()


def get_equip_loja(conn, loja_id):
    return get_config(conn, f"equip_loja_{loja_id}", "") or ""


def mapa_equip_loja(conn):
    """{EquipmentNumber(str): loja_id} das catracas de ENTRADA — usado para
    rotear o acesso e colocar o motoboy na fila da unidade certa."""
    m = {}
    for lj in conn.execute("SELECT id FROM lojas").fetchall():
        v = get_config(conn, f"equip_loja_{lj['id']}", "") or ""
        for e in v.replace(";", ",").split(","):
            e = e.strip()
            if e:
                m[e] = lj["id"]
    return m


def set_equip_saida(conn, loja_id, equipamentos):
    """Define quais catracas (EquipmentNumber) são de SAÍDA desta loja.
    Ao passar numa catraca de saída, o motoboy sai da fila."""
    set_config(conn, f"equip_saida_{loja_id}", (equipamentos or "").strip())
    conn.commit()


def get_equip_saida(conn, loja_id):
    return get_config(conn, f"equip_saida_{loja_id}", "") or ""


def mapa_equip_saida(conn):
    """{EquipmentNumber(str): loja_id} das catracas de SAÍDA."""
    m = {}
    for lj in conn.execute("SELECT id FROM lojas").fetchall():
        v = get_config(conn, f"equip_saida_{lj['id']}", "") or ""
        for e in v.replace(";", ",").split(","):
            e = e.strip()
            if e:
                m[e] = lj["id"]
    return m


# ---- Prestação de contas: documentos obrigatórios + justificativas --------

def set_docs_obrigatorios(conn, lista, ol_id=None):
    """Documentos obrigatórios por competência (definidos pelo admin).
    ol_id=None → PADRÃO GERAL (vale para toda OL sem config própria);
    ol_id preenchido → lista específica daquela OL (sobrepõe o padrão)."""
    chave = "docs_obrigatorios" if ol_id is None else f"docs_obrigatorios_{ol_id}"
    set_config(conn, chave, "||".join(lista or []))
    conn.commit()


def tem_docs_obrigatorios_proprios(conn, ol_id):
    """True se a OL tem uma lista PRÓPRIA configurada (mesmo que vazia)."""
    return get_config(conn, f"docs_obrigatorios_{ol_id}", None) is not None


def remover_docs_obrigatorios_ol(conn, ol_id):
    """Remove a lista própria da OL → volta a herdar o Padrão geral."""
    conn.execute("DELETE FROM configuracoes WHERE chave=?", (f"docs_obrigatorios_{ol_id}",))
    conn.commit()


# ===========================================================================
# FATURAMENTO: faturas por OL/competência + itens (entregadores) + boleto/NF
# ===========================================================================

_TABELAS_FATURAS_OK = False


def garantir_tabelas_faturas(conn):
    """Cria (se faltarem) as tabelas de faturamento. Idempotente."""
    global _TABELAS_FATURAS_OK
    if _TABELAS_FATURAS_OK:
        return
    pg = usando_pg()
    serial = "SERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    blob = "BYTEA" if pg else "BLOB"
    val = "NUMERIC(12,2)" if pg else "REAL"
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS faturas ("
        f" id {serial}, ol_id INTEGER NOT NULL REFERENCES ols(id), competencia TEXT NOT NULL,"
        f" valor_calculado {val} DEFAULT 0, valor_final {val} DEFAULT 0,"
        f" status TEXT NOT NULL DEFAULT 'rascunho',"          # rascunho/aprovada/reprovada
        f" motivo TEXT, vencimento TEXT,"
        f" aprovada_admin INTEGER NOT NULL DEFAULT 0, aprovada_fin INTEGER NOT NULL DEFAULT 0,"
        f" boleto_nome TEXT, boleto_mime TEXT, boleto_arquivo {blob}, boleto_valor {val},"
        f" nf_nome TEXT, nf_mime TEXT, nf_arquivo {blob}, nf_valor {val},"
        f" conf_valor TEXT, enviado_forms INTEGER NOT NULL DEFAULT 0, forms_em TEXT,"
        f" criado_em TEXT, atualizado_em TEXT,"
        f" UNIQUE (ol_id, competencia))")
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS fatura_itens ("
        f" id {serial}, fatura_id INTEGER NOT NULL REFERENCES faturas(id),"
        f" motoboy_id INTEGER, nome TEXT, dias INTEGER DEFAULT 0,"
        f" valor_diaria {val} DEFAULT 0, bonificacao {val} DEFAULT 0, subtotal {val} DEFAULT 0)")
    conn.commit()
    _TABELAS_FATURAS_OK = True


def set_valor_diaria(conn, valor, ol_id=None):
    """Valor da diária por entregador. ol_id=None → padrão geral; senão, da OL."""
    chave = "valor_diaria" if ol_id is None else f"valor_diaria_{ol_id}"
    set_config(conn, chave, str(valor or 0))
    conn.commit()


def get_valor_diaria(conn, ol_id=None):
    if ol_id is not None:
        v = get_config(conn, f"valor_diaria_{ol_id}", None)
        if v is not None:
            try: return float(v)
            except ValueError: return 0.0
    v = get_config(conn, "valor_diaria", "0") or "0"
    try: return float(v)
    except ValueError: return 0.0


def get_fatura(conn, ol_id, competencia):
    garantir_tabelas_faturas(conn)
    return conn.execute("SELECT * FROM faturas WHERE ol_id=? AND competencia=?",
                        (ol_id, competencia)).fetchone()


def get_fatura_id(conn, fatura_id):
    return conn.execute("SELECT * FROM faturas WHERE id=?", (fatura_id,)).fetchone()


def itens_fatura(conn, fatura_id):
    return conn.execute(
        "SELECT * FROM fatura_itens WHERE fatura_id=? ORDER BY nome", (fatura_id,)).fetchall()


def gerar_fatura(conn, ol_id, competencia, criado_por=None):
    """Gera (ou regenera, se ainda em rascunho) a fatura da OL na competência:
    um item por entregador ATIVO, com dias trabalhados (passagens de entrada no
    mês) × diária. Preserva a bonificação já lançada. Não mexe se já aprovada."""
    garantir_tabelas_faturas(conn)
    fat = get_fatura(conn, ol_id, competencia)
    if fat and fat["status"] == "aprovada":
        return fat["id"]
    diaria = get_valor_diaria(conn, ol_id)
    agora = _agora_br()
    if not fat:
        if usando_pg():
            fid = conn.execute(
                "INSERT INTO faturas (ol_id, competencia, status, criado_em, atualizado_em) "
                "VALUES (?,?,'rascunho',?,?) RETURNING id",
                (ol_id, competencia, agora, agora)).fetchone()[0]
        else:
            fid = conn.execute(
                "INSERT INTO faturas (ol_id, competencia, status, criado_em, atualizado_em) "
                "VALUES (?,?,'rascunho',?,?)", (ol_id, competencia, agora, agora)).lastrowid
        bonif_ant = {}
    else:
        fid = fat["id"]
        bonif_ant = {r["motoboy_id"]: float(r["bonificacao"] or 0)
                     for r in itens_fatura(conn, fid)}
        conn.execute("DELETE FROM fatura_itens WHERE fatura_id=?", (fid,))
    mbs = conn.execute(
        "SELECT DISTINCT m.id, m.nome FROM cadastros c JOIN motoboys m ON m.id=c.motoboy_id "
        "WHERE c.ol_id=? AND c.situacao='ativo' ORDER BY m.nome", (ol_id,)).fetchall()
    total = 0.0
    for m in mbs:
        dias = conn.execute(
            "SELECT COUNT(DISTINCT substr(ocorrido_em,1,10)) FROM acesso_eventos "
            "WHERE motoboy_id=? AND tipo='entrada' AND ocorrido_em LIKE ?",
            (m["id"], competencia + "%")).fetchone()[0] or 0
        bonif = bonif_ant.get(m["id"], 0.0)
        subtotal = dias * diaria + bonif
        conn.execute(
            "INSERT INTO fatura_itens (fatura_id, motoboy_id, nome, dias, valor_diaria, "
            "bonificacao, subtotal) VALUES (?,?,?,?,?,?,?)",
            (fid, m["id"], m["nome"], dias, diaria, bonif, subtotal))
        total += subtotal
    conn.execute("UPDATE faturas SET valor_calculado=?, valor_final=?, atualizado_em=? WHERE id=?",
                 (total, total, agora, fid))
    conn.commit()
    return fid


def atualizar_item_fatura(conn, item_id, dias, valor_diaria, bonificacao):
    """Ajusta um item (admin/financeiro) e recalcula o subtotal + total da fatura."""
    dias = int(dias or 0); vd = float(valor_diaria or 0); bn = float(bonificacao or 0)
    sub = dias * vd + bn
    row = conn.execute("SELECT fatura_id FROM fatura_itens WHERE id=?", (item_id,)).fetchone()
    conn.execute("UPDATE fatura_itens SET dias=?, valor_diaria=?, bonificacao=?, subtotal=? WHERE id=?",
                 (dias, vd, bn, sub, item_id))
    if row:
        _recalcular_fatura(conn, row["fatura_id"])
    conn.commit()


def set_bonificacao_item(conn, item_id, bonificacao):
    """Atalho: lança só a bonificação de um entregador (admin)."""
    it = conn.execute("SELECT dias, valor_diaria FROM fatura_itens WHERE id=?",
                      (item_id,)).fetchone()
    if it:
        atualizar_item_fatura(conn, item_id, it["dias"], it["valor_diaria"], bonificacao)


def _recalcular_fatura(conn, fatura_id):
    tot = conn.execute("SELECT COALESCE(SUM(subtotal),0) FROM fatura_itens WHERE fatura_id=?",
                       (fatura_id,)).fetchone()[0] or 0
    conn.execute("UPDATE faturas SET valor_final=?, atualizado_em=? WHERE id=?",
                 (float(tot), _agora_br(), fatura_id))


def aprovar_fatura(conn, fatura_id, perfil, por=None):
    """Aprovação da fatura por admin e/ou financeiro. Só vira 'aprovada' quando
    AMBOS aprovarem (admin + financeiro)."""
    col = "aprovada_admin" if perfil == "admin" else "aprovada_fin"
    conn.execute(f"UPDATE faturas SET {col}=1, motivo=NULL, atualizado_em=? WHERE id=?",
                 (_agora_br(), fatura_id))
    f = get_fatura_id(conn, fatura_id)
    if f and f["aprovada_admin"] and f["aprovada_fin"]:
        conn.execute("UPDATE faturas SET status='aprovada' WHERE id=?", (fatura_id,))
    conn.commit()


def reprovar_fatura(conn, fatura_id, motivo, por=None):
    conn.execute("UPDATE faturas SET status='reprovada', motivo=?, aprovada_admin=0, "
                 "aprovada_fin=0, atualizado_em=? WHERE id=?",
                 (motivo, _agora_br(), fatura_id))
    conn.commit()


def set_vencimento_fatura(conn, fatura_id, data):
    conn.execute("UPDATE faturas SET vencimento=?, atualizado_em=? WHERE id=?",
                 (str(data) if data else None, _agora_br(), fatura_id))
    conn.commit()


def anexar_boleto(conn, fatura_id, nome, mime, dados, valor):
    conn.execute("UPDATE faturas SET boleto_nome=?, boleto_mime=?, boleto_arquivo=?, "
                 "boleto_valor=?, atualizado_em=? WHERE id=?",
                 (nome, mime, dados, float(valor or 0), _agora_br(), fatura_id))
    conn.commit()
    conferir_valor_fatura(conn, fatura_id)


def anexar_nf(conn, fatura_id, nome, mime, dados, valor):
    conn.execute("UPDATE faturas SET nf_nome=?, nf_mime=?, nf_arquivo=?, nf_valor=?, "
                 "atualizado_em=? WHERE id=?",
                 (nome, mime, dados, float(valor or 0), _agora_br(), fatura_id))
    conn.commit()
    conferir_valor_fatura(conn, fatura_id)


def conferir_valor_fatura(conn, fatura_id):
    """Confere automaticamente se o valor do boleto bate com o valor da fatura.
    Grava 'ok' ou 'divergente' em conf_valor."""
    f = get_fatura_id(conn, fatura_id)
    if not f or f["boleto_valor"] is None:
        return None
    ok = abs(float(f["boleto_valor"]) - float(f["valor_final"] or 0)) < 0.01
    res = "ok" if ok else "divergente"
    conn.execute("UPDATE faturas SET conf_valor=? WHERE id=?", (res, fatura_id))
    conn.commit()
    return res


def marcar_enviado_forms(conn, fatura_id):
    conn.execute("UPDATE faturas SET enviado_forms=1, forms_em=? WHERE id=?",
                 (_agora_br(), fatura_id))
    conn.commit()


def listar_faturas(conn, competencia=None):
    garantir_tabelas_faturas(conn)
    if competencia:
        return conn.execute(
            "SELECT f.*, o.nome AS ol FROM faturas f JOIN ols o ON o.id=f.ol_id "
            "WHERE f.competencia=? ORDER BY o.nome", (competencia,)).fetchall()
    return conn.execute(
        "SELECT f.*, o.nome AS ol FROM faturas f JOIN ols o ON o.id=f.ol_id "
        "ORDER BY f.competencia DESC, o.nome").fetchall()


def progresso_conferencia(conn, ol_id, competencia):
    """Progresso da conferência de documentos da OL na competência:
    {total, validados, reprovados, pendentes, pct}. 'validados'=aprovados."""
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM prestacao_documentos "
        "WHERE ol_id=? AND competencia=? GROUP BY status", (ol_id, competencia)).fetchall()
    by = {r["status"]: r["n"] for r in rows}
    total = sum(by.values())
    validados = by.get("aprovado", 0) + by.get("validado", 0)
    reprovados = by.get("rejeitado", 0) + by.get("reprovado", 0)
    pendentes = total - validados - reprovados
    pct = (validados / total) if total else 0.0
    return {"total": total, "validados": validados, "reprovados": reprovados,
            "pendentes": pendentes, "pct": pct}


def get_docs_obrigatorios(conn, ol_id=None):
    """Lista efetiva de documentos obrigatórios. Se a OL tiver config própria,
    usa a dela; senão, cai no PADRÃO GERAL."""
    if ol_id is not None:
        v = get_config(conn, f"docs_obrigatorios_{ol_id}", None)
        if v is not None:                # OL tem config própria (pode ser vazia)
            return [x for x in v.split("||") if x]
    v = get_config(conn, "docs_obrigatorios", "") or ""
    return [x for x in v.split("||") if x]


def salvar_justificativa(conn, ol_id, competencia, motoboy_id, tipo, texto, criado_por):
    """OL justifica o NÃO envio de um documento. Fica 'pendente' até o financeiro
    aprovar/reprovar. Reenviar a justificativa volta ao estado 'pendente'."""
    ex = conn.execute(
        "SELECT id FROM prestacao_justificativas WHERE ol_id=? AND competencia=? "
        "AND motoboy_id=? AND tipo=?", (ol_id, competencia, motoboy_id, tipo)).fetchone()
    if ex:
        conn.execute(
            "UPDATE prestacao_justificativas SET texto=?, status='pendente', "
            "motivo_reprovacao=NULL, decidido_por=NULL, decidido_em=NULL WHERE id=?",
            (texto, ex["id"]))
    else:
        conn.execute(
            "INSERT INTO prestacao_justificativas "
            "(ol_id, competencia, motoboy_id, tipo, texto, status, criado_por) "
            "VALUES (?,?,?,?,?, 'pendente', ?)",
            (ol_id, competencia, motoboy_id, tipo, texto, criado_por))
    conn.commit()


def justificativas_da_ol(conn, ol_id, competencia):
    """{(motoboy_id, tipo): row} das justificativas dessa OL/competência."""
    rows = conn.execute(
        "SELECT id, motoboy_id, tipo, texto, status, motivo_reprovacao "
        "FROM prestacao_justificativas WHERE ol_id=? AND competencia=?",
        (ol_id, competencia)).fetchall()
    return {(r["motoboy_id"], r["tipo"]): r for r in rows}


def decidir_justificativa(conn, just_id, aprovar, motivo, decidido_por):
    """Financeiro aprova/reprova uma justificativa de não envio."""
    conn.execute(
        "UPDATE prestacao_justificativas SET status=?, motivo_reprovacao=?, "
        "decidido_por=?, decidido_em=? WHERE id=?",
        ("aprovada" if aprovar else "reprovada", None if aprovar else motivo,
         decidido_por, _agora_br(), just_id))
    conn.commit()


def enviados_por_motoboy_tipo(conn, ol_id, competencia):
    """{(motoboy_id, tipo)} que já têm documento enviado nessa competência."""
    rows = conn.execute(
        "SELECT DISTINCT pv.motoboy_id, pv.tipo FROM prestacao_valores pv "
        "JOIN prestacao_documentos pd ON pd.id = pv.documento_id "
        "WHERE pd.ol_id=? AND pd.competencia=? AND pv.motoboy_id IS NOT NULL",
        (ol_id, competencia)).fetchall()
    return {(r["motoboy_id"], r["tipo"]) for r in rows}


def justificativas_pendentes(conn):
    """Justificativas de não envio aguardando análise do financeiro."""
    return conn.execute(
        "SELECT j.id, j.ol_id, o.nome AS ol, j.competencia, j.motoboy_id, "
        "m.nome AS motoboy, j.tipo, j.texto, j.criado_em "
        "FROM prestacao_justificativas j "
        "JOIN ols o ON o.id=j.ol_id LEFT JOIN motoboys m ON m.id=j.motoboy_id "
        "WHERE j.status='pendente' ORDER BY j.criado_em").fetchall()


def enviar_mensagem_ol(conn, ol_id, texto, competencia=None, criado_por=None):
    conn.execute(
        "INSERT INTO mensagens_ol (ol_id, competencia, texto, criado_por) VALUES (?,?,?,?)",
        (ol_id, competencia, texto, criado_por))
    conn.commit()


def mensagens_da_ol(conn, ol_id, limite=20):
    return conn.execute(
        "SELECT id, competencia, texto, lido, criado_em FROM mensagens_ol "
        "WHERE ol_id=? ORDER BY id DESC LIMIT ?", (ol_id, limite)).fetchall()


def marcar_mensagens_lidas(conn, ol_id):
    conn.execute("UPDATE mensagens_ol SET lido=1 WHERE ol_id=? AND lido=0", (ol_id,))
    conn.commit()


def docs_reprovados_ol(conn, ol_id, competencia):
    """Documentos (envios) reprovados pelo financeiro nessa OL/competência."""
    return conn.execute(
        "SELECT id, tipo, competencia FROM prestacao_documentos "
        "WHERE ol_id=? AND competencia=? AND status='rejeitado' ORDER BY id DESC",
        (ol_id, competencia)).fetchall()


# ---- Fila FIFO de expedição (painel do operador) --------------------------

def registrar_chegada(conn, loja_id, motoboy_id, chegada_em=None) -> bool:
    """Coloca o motoboy na fila de expedição da loja (ordem = hora de chegada).
    Não duplica: se já está aguardando nesta loja, devolve False. `chegada_em`
    permite usar a hora do evento da catraca (senão usa agora, horário BR)."""
    ja = conn.execute(
        "SELECT id FROM fila_expedicao WHERE loja_id=? AND motoboy_id=? AND situacao='aguardando'",
        (loja_id, motoboy_id)).fetchone()
    if ja:
        return False
    conn.execute(
        "INSERT INTO fila_expedicao (loja_id, motoboy_id, chegada_em, situacao) "
        "VALUES (?,?,?, 'aguardando')", (loja_id, motoboy_id, chegada_em or _agora_br()))
    conn.commit()
    return True


def fila_aguardando(conn, loja_id):
    """Fila FIFO (quem chegou primeiro vem primeiro)."""
    return conn.execute(
        "SELECT f.id, f.motoboy_id, f.chegada_em, m.nome, m.cpf, "
        "(SELECT placa FROM motoboys_ol WHERE motoboy_id=f.motoboy_id LIMIT 1) AS placa "
        "FROM fila_expedicao f JOIN motoboys m ON m.id=f.motoboy_id "
        "WHERE f.loja_id=? AND f.situacao='aguardando' "
        "ORDER BY f.chegada_em ASC, f.id ASC", (loja_id,)).fetchall()


def despachar_fila(conn, fila_id):
    """Libera a entrega (tira da fila, marcando como despachado)."""
    conn.execute("UPDATE fila_expedicao SET situacao='despachado', despachado_em=? WHERE id=?",
                 (_agora_br(), fila_id))
    conn.commit()


def remover_fila(conn, fila_id):
    """Remove o motoboy da fila sem despachar (ex.: saiu, engano)."""
    conn.execute("DELETE FROM fila_expedicao WHERE id=?", (fila_id,))
    conn.commit()


def sair_da_fila(conn, loja_id, motoboy_id) -> bool:
    """Motoboy saiu pela catraca de saída: se estiver aguardando nesta loja,
    marca como despachado (saiu com o pedido). Devolve True se removeu alguém.
    Quando ele voltar e passar na entrada, reentra na fila (evento novo)."""
    row = conn.execute(
        "SELECT id FROM fila_expedicao WHERE loja_id=? AND motoboy_id=? "
        "AND situacao='aguardando' ORDER BY chegada_em ASC, id ASC LIMIT 1",
        (loja_id, motoboy_id)).fetchone()
    if not row:
        return False
    conn.execute("UPDATE fila_expedicao SET situacao='despachado', despachado_em=? WHERE id=?",
                 (_agora_br(), row["id"]))
    conn.commit()
    return True


def limpar_fila(conn, loja_id):
    """Esvazia a fila de aguardando desta loja (recomeçar um teste)."""
    conn.execute("DELETE FROM fila_expedicao WHERE loja_id=? AND situacao='aguardando'",
                 (loja_id,))
    conn.commit()


# ---- Dados brutos para RELATÓRIOS / KPIs (histórico da catraca) ------------

def eventos_periodo(conn, ini, fim, loja_id=None):
    """Passagens registradas (acesso_eventos) no período [ini, fim) — strings
    'AAAA-MM-DD HH:MM:SS'. Base dos relatórios de chegada/saída/duração."""
    q = ("SELECT e.id, e.motoboy_id, e.nome, e.cpf, e.loja_id, e.tipo, e.ocorrido_em, "
         "l.nome AS loja FROM acesso_eventos e LEFT JOIN lojas l ON l.id=e.loja_id "
         "WHERE e.ocorrido_em >= ? AND e.ocorrido_em < ?")
    p = [ini, fim]
    if loja_id:
        q += " AND e.loja_id=?"
        p.append(loja_id)
    q += " ORDER BY e.ocorrido_em ASC, e.id ASC"
    return conn.execute(q, tuple(p)).fetchall()


def fila_periodo(conn, ini, fim, loja_id=None):
    """Registros da fila FIFO (fila_expedicao) no período (por chegada_em) —
    base do tempo de espera na fila e do nº de despachos (entregas)."""
    q = ("SELECT f.id, f.motoboy_id, m.nome, f.loja_id, f.chegada_em, f.despachado_em, "
         "f.situacao, l.nome AS loja FROM fila_expedicao f "
         "JOIN motoboys m ON m.id=f.motoboy_id LEFT JOIN lojas l ON l.id=f.loja_id "
         "WHERE f.chegada_em >= ? AND f.chegada_em < ?")
    p = [ini, fim]
    if loja_id:
        q += " AND f.loja_id=?"
        p.append(loja_id)
    q += " ORDER BY f.chegada_em ASC, f.id ASC"
    return conn.execute(q, tuple(p)).fetchall()


def despachados_recentes(conn, loja_id, limite=10):
    """Últimas entregas liberadas nesta loja (histórico curto)."""
    return conn.execute(
        "SELECT m.nome, f.chegada_em, f.despachado_em FROM fila_expedicao f "
        "JOIN motoboys m ON m.id=f.motoboy_id "
        "WHERE f.loja_id=? AND f.situacao='despachado' "
        "ORDER BY f.despachado_em DESC LIMIT ?", (loja_id, limite)).fetchall()


def salvar_video_treinamento(conn, nome, mime, dados):
    """Guarda o vídeo de treinamento (mantém só o mais recente)."""
    conn.execute("DELETE FROM treinamento_video")
    conn.execute("INSERT INTO treinamento_video (nome_arquivo, mime, dados) VALUES (?,?,?)",
                 (nome, mime, dados))
    conn.commit()


def get_video_treinamento(conn):
    """Vídeo de treinamento atual (ou None)."""
    return conn.execute(
        "SELECT id, nome_arquivo, mime, dados, criado_em FROM treinamento_video "
        "ORDER BY id DESC LIMIT 1").fetchone()


def remover_video_treinamento(conn):
    conn.execute("DELETE FROM treinamento_video")
    conn.commit()


def marcar_treinamento_visto(conn, motoboy_id):
    """Marca que o motoboy já assistiu ao treinamento (1ª vez, por CPF)."""
    conn.execute("UPDATE motoboys SET treinamento_em=? WHERE id=?",
                 (_agora_br(), motoboy_id))
    conn.commit()


_TABELAS_PRESTACAO_OK = False


def garantir_tabelas_prestacao(conn):
    """Cria (se faltarem) as tabelas de prestação de contas e configurações.
    Roda só UMA vez por processo (evita DDL a cada abertura de tela — deixa o app
    muito mais rápido no PostgreSQL, onde cada comando é uma ida ao servidor)."""
    global _TABELAS_PRESTACAO_OK
    pg = usando_pg()
    serial = "SERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    ts = "to_char(now(), 'YYYY-MM-DD HH24:MI:SS')" if pg else "CURRENT_TIMESTAMP"
    # Tabelas independentes (justificativas de não envio + mensagens à OL): SEMPRE
    # garante (CREATE IF NOT EXISTS é barato/idempotente) — robusto quando o app
    # recarrega o código sem recriar o schema (evita UndefinedTable).
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS prestacao_justificativas ("
        f" id {serial}, ol_id INTEGER NOT NULL, competencia TEXT NOT NULL,"
        f" motoboy_id INTEGER, tipo TEXT NOT NULL, texto TEXT,"
        f" status TEXT NOT NULL DEFAULT 'pendente', motivo_reprovacao TEXT,"
        f" criado_por INTEGER, decidido_por INTEGER, decidido_em TEXT,"
        f" criado_em TEXT NOT NULL DEFAULT {ts})")
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS mensagens_ol ("
        f" id {serial}, ol_id INTEGER NOT NULL, competencia TEXT, texto TEXT NOT NULL,"
        f" lido INTEGER NOT NULL DEFAULT 0, criado_por INTEGER,"
        f" criado_em TEXT NOT NULL DEFAULT {ts})")
    conn.commit()
    # motoboy_nome (nome livre, motoboy não cadastrado): a migração principal fica
    # atrás da trava 1x/processo; quando o processo é reaproveitado após um deploy,
    # ela não roda e dá UndefinedColumn. Garante SEMPRE (idempotente). Se a tabela
    # ainda não existe (1º boot), o except ignora — a criação vem logo abaixo.
    for _cc in ("motoboy_nome TEXT", "periodo_ini TEXT", "periodo_fim TEXT"):
        try:
            if pg:
                conn.execute(f"ALTER TABLE prestacao_documentos ADD COLUMN IF NOT EXISTS {_cc}")
            else:
                conn.execute(f"ALTER TABLE prestacao_documentos ADD COLUMN {_cc}")
            conn.commit()
        except Exception:
            pass
    if _TABELAS_PRESTACAO_OK:
        return
    blob = "BYTEA" if pg else "BLOB"
    valor_tipo = "NUMERIC(12,2)" if pg else "REAL"
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS prestacao_documentos ("
        f" id {serial}, ol_id INTEGER NOT NULL REFERENCES ols(id), tipo TEXT NOT NULL,"
        f" competencia TEXT, escopo TEXT NOT NULL DEFAULT 'individual',"
        f" nome_arquivo TEXT, mime TEXT, arquivo {blob},"
        f" status TEXT NOT NULL DEFAULT 'pendente',"
        f" criado_por INTEGER REFERENCES usuarios(id),"
        f" criado_em TEXT NOT NULL DEFAULT {ts})")
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS prestacao_valores ("
        f" id {serial}, documento_id INTEGER NOT NULL REFERENCES prestacao_documentos(id),"
        f" motoboy_id INTEGER REFERENCES motoboys(id), tipo TEXT, valor {valor_tipo})")
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS prestacao_arquivos ("
        f" id {serial}, documento_id INTEGER NOT NULL REFERENCES prestacao_documentos(id),"
        f" nome_arquivo TEXT, mime TEXT, arquivo {blob})")
    conn.execute("CREATE TABLE IF NOT EXISTS configuracoes (chave TEXT PRIMARY KEY, valor TEXT)")
    # (prestacao_justificativas e mensagens_ol já são criadas no topo desta função.)
    # Migração: adiciona a coluna 'tipo' em bancos que já tinham prestacao_valores sem ela.
    # Colunas de validação (perfil financeiro): checklist + status + observação + autoria.
    _cols_valores = [("tipo", "TEXT")]
    # Colunas de validação: existem TANTO no documento (envio) quanto em cada
    # arquivo — a validação é feita por arquivo (independente), e o status do
    # documento passa a ser o resumo dos seus arquivos.
    _cols_val = [
        ("val_legivel", "INTEGER DEFAULT 0"),     # checklist: documento legível
        ("val_assinatura", "INTEGER DEFAULT 0"),  # checklist: assinatura confere
        ("val_valor", "INTEGER DEFAULT 0"),       # checklist: valor confere
        ("obs_validacao", "TEXT"),                # observação do financeiro
        ("validado_por", "INTEGER"),              # usuário financeiro que validou
        ("validado_em", "TEXT"),                  # data/hora da validação
    ]
    _cols_arqs = [("status", "TEXT DEFAULT 'pendente'")] + _cols_val
    # motoboy_nome: nome digitado quando o motoboy NÃO está cadastrado (uso da
    # conferência antecipada, antes de as OLs cadastrarem).
    _cols_doc = _cols_val + [("motoboy_nome", "TEXT"),
                             ("periodo_ini", "TEXT"), ("periodo_fim", "TEXT")]
    for tabela, colunas in (("prestacao_valores", _cols_valores),
                            ("prestacao_documentos", _cols_doc),
                            ("prestacao_arquivos", _cols_arqs)):
        for nome_col, tipo_col in colunas:
            if pg:
                # PG: IF NOT EXISTS evita erro que abortaria a transação.
                conn.execute(f"ALTER TABLE {tabela} ADD COLUMN IF NOT EXISTS {nome_col} {tipo_col}")
            else:
                try:
                    conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {nome_col} {tipo_col}")
                except Exception:
                    pass
    conn.commit()
    _TABELAS_PRESTACAO_OK = True


def get_config(conn, chave, default=None):
    r = conn.execute("SELECT valor FROM configuracoes WHERE chave=?", (chave,)).fetchone()
    return r["valor"] if r else default


def set_config(conn, chave, valor):
    if usando_pg():
        conn.execute("INSERT INTO configuracoes (chave, valor) VALUES (?,?) "
                     "ON CONFLICT (chave) DO UPDATE SET valor=EXCLUDED.valor", (chave, valor))
    else:
        conn.execute("INSERT INTO configuracoes (chave, valor) VALUES (?,?) "
                     "ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor", (chave, valor))


def salvar_prestacao(conn, ol_id, tipo, competencia, escopo, arquivos, valores,
                     criado_por, motoboy_nome=None, periodo_ini=None, periodo_fim=None):
    """Salva um documento de prestação de contas (com 1+ arquivos) + os valores.
    arquivos: lista de tuplas (nome, mime, bytes).
    valores:  lista de tuplas (motoboy_id, tipo, valor).
    motoboy_nome: nome livre do motoboy quando ele NÃO está cadastrado.
    periodo_ini/periodo_fim: intervalo de datas (AAAA-MM-DD) de referência.
    Devolve o id do documento."""
    primeiro_nome = arquivos[0][0] if arquivos else None
    primeiro_mime = arquivos[0][1] if arquivos else None
    doc_id = _inserir_id(
        conn,
        "INSERT INTO prestacao_documentos "
        "(ol_id, tipo, competencia, escopo, nome_arquivo, mime, criado_por, motoboy_nome, "
        "periodo_ini, periodo_fim) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ol_id, tipo, competencia, escopo, primeiro_nome, primeiro_mime, criado_por,
         motoboy_nome, periodo_ini, periodo_fim))
    for nome, mime, dados in arquivos:
        conn.execute(
            "INSERT INTO prestacao_arquivos (documento_id, nome_arquivo, mime, arquivo) "
            "VALUES (?,?,?,?)", (doc_id, nome, mime, dados))
    for motoboy_id, tipo_item, valor in valores:
        conn.execute(
            "INSERT INTO prestacao_valores (documento_id, motoboy_id, tipo, valor) "
            "VALUES (?,?,?,?)", (doc_id, motoboy_id, tipo_item, valor))
    return doc_id


def _agora_br():
    from datetime import datetime, timezone, timedelta
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")


def validar_documento(conn, doc_id, legivel, assinatura, valor_ok, status, obs, validado_por):
    """Registra a validação de um documento pelo financeiro (checklist + status).
    Usado para documentos LEGADOS (1 arquivo embutido no próprio documento).
    status: 'validado' | 'rejeitado' | 'pendente'. Guarda a autoria e o horário (BR)."""
    conn.execute(
        "UPDATE prestacao_documentos SET val_legivel=?, val_assinatura=?, val_valor=?, "
        "status=?, obs_validacao=?, validado_por=?, validado_em=? WHERE id=?",
        (1 if legivel else 0, 1 if assinatura else 0, 1 if valor_ok else 0,
         status, obs, validado_por, _agora_br(), doc_id))
    conn.commit()


def validar_arquivo(conn, arquivo_id, legivel, assinatura, valor_ok, status, obs, validado_por):
    """Valida UM arquivo (documento) de um envio, de forma independente dos demais.
    Depois recalcula o status do envio (documento-pai): rejeitado se qualquer arquivo
    foi rejeitado; validado se TODOS foram validados; senão pendente."""
    conn.execute(
        "UPDATE prestacao_arquivos SET val_legivel=?, val_assinatura=?, val_valor=?, "
        "status=?, obs_validacao=?, validado_por=?, validado_em=? WHERE id=?",
        (1 if legivel else 0, 1 if assinatura else 0, 1 if valor_ok else 0,
         status, obs, validado_por, _agora_br(), arquivo_id))
    linha = conn.execute("SELECT documento_id FROM prestacao_arquivos WHERE id=?",
                         (arquivo_id,)).fetchone()
    if linha:
        did = linha[0]
        sts = [(r[0] or "pendente") for r in conn.execute(
            "SELECT status FROM prestacao_arquivos WHERE documento_id=?", (did,)).fetchall()]
        if any(s == "rejeitado" for s in sts):
            resumo = "rejeitado"
        elif any(s == "correcao" for s in sts):
            resumo = "correcao"
        elif sts and all(s == "validado" for s in sts):
            resumo = "validado"
        else:
            resumo = "pendente"
        conn.execute("UPDATE prestacao_documentos SET status=? WHERE id=?", (resumo, did))
    conn.commit()
