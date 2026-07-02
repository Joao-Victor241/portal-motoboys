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
    "admin":      ("ADMIN_PASSWORD", "admin123"),
    "ol_exemplo": ("OL_EXEMPLO_PASSWORD", "ol123"),
    "operador":   ("OPERADOR_PASSWORD", "op123"),
    "financeiro": ("FINANCEIRO_PASSWORD", "fin123"),
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
        # Remove o CHECK antigo de perfil (não incluía 'financeiro') antes de inserir.
        try:
            conn.execute("ALTER TABLE usuarios DROP CONSTRAINT IF EXISTS usuarios_perfil_check")
        except Exception:
            pass
        if conn.execute("SELECT COUNT(*) FROM usuarios WHERE login='financeiro'").fetchone()[0] == 0:
            conn.execute("INSERT INTO usuarios (login, senha_hash, perfil) VALUES (?,?,?)",
                         ("financeiro", _hash(_senha_padrao("financeiro")), "financeiro"))
        # Sincroniza senhas com os Secrets (1x por processo).
        if not _SENHAS_SINCRONIZADAS:
            for login, (chave, _fb) in _MAPA_SENHA_ENV.items():
                if os.getenv(chave):
                    conn.execute("UPDATE usuarios SET senha_hash=? WHERE login=?",
                                 (_hash(_senha_padrao(login)), login))
            _SENHAS_SINCRONIZADAS = True
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

    # Sincroniza as senhas dos usuários padrão com os Secrets (1x por processo).
    # Só atualiza quando a variável de ambiente estiver definida — assim, definir
    # ADMIN_PASSWORD/etc. nos Secrets passa a valer mesmo se o banco já existia.
    if not _SENHAS_SINCRONIZADAS:
        for login, (chave, _fb) in _MAPA_SENHA_ENV.items():
            if os.getenv(chave):
                conn.execute("UPDATE usuarios SET senha_hash=? WHERE login=?",
                             (_hash(_senha_padrao(login)), login))
        _SENHAS_SINCRONIZADAS = True

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


_TABELAS_PRESTACAO_OK = False


def garantir_tabelas_prestacao(conn):
    """Cria (se faltarem) as tabelas de prestação de contas e configurações.
    Roda só UMA vez por processo (evita DDL a cada abertura de tela — deixa o app
    muito mais rápido no PostgreSQL, onde cada comando é uma ida ao servidor)."""
    global _TABELAS_PRESTACAO_OK
    if _TABELAS_PRESTACAO_OK:
        return
    pg = usando_pg()
    serial = "SERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    blob = "BYTEA" if pg else "BLOB"
    ts = "to_char(now(), 'YYYY-MM-DD HH24:MI:SS')" if pg else "CURRENT_TIMESTAMP"
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
    # Migração: adiciona a coluna 'tipo' em bancos que já tinham prestacao_valores sem ela.
    # Colunas de validação (perfil financeiro): checklist + status + observação + autoria.
    _cols_valores = [("tipo", "TEXT")]
    _cols_docs = [
        ("val_legivel", "INTEGER DEFAULT 0"),     # checklist: documento legível
        ("val_assinatura", "INTEGER DEFAULT 0"),  # checklist: assinatura confere
        ("val_valor", "INTEGER DEFAULT 0"),       # checklist: valor confere
        ("obs_validacao", "TEXT"),                # observação do financeiro
        ("validado_por", "INTEGER"),              # usuário financeiro que validou
        ("validado_em", "TEXT"),                  # data/hora da validação
    ]
    for tabela, colunas in (("prestacao_valores", _cols_valores),
                            ("prestacao_documentos", _cols_docs)):
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


def salvar_prestacao(conn, ol_id, tipo, competencia, escopo, arquivos, valores, criado_por):
    """Salva um documento de prestação de contas (com 1+ arquivos) + os valores.
    arquivos: lista de tuplas (nome, mime, bytes).
    valores:  lista de tuplas (motoboy_id, tipo, valor).
    Devolve o id do documento."""
    primeiro_nome = arquivos[0][0] if arquivos else None
    primeiro_mime = arquivos[0][1] if arquivos else None
    doc_id = _inserir_id(
        conn,
        "INSERT INTO prestacao_documentos "
        "(ol_id, tipo, competencia, escopo, nome_arquivo, mime, criado_por) "
        "VALUES (?,?,?,?,?,?,?)",
        (ol_id, tipo, competencia, escopo, primeiro_nome, primeiro_mime, criado_por))
    for nome, mime, dados in arquivos:
        conn.execute(
            "INSERT INTO prestacao_arquivos (documento_id, nome_arquivo, mime, arquivo) "
            "VALUES (?,?,?,?)", (doc_id, nome, mime, dados))
    for motoboy_id, tipo_item, valor in valores:
        conn.execute(
            "INSERT INTO prestacao_valores (documento_id, motoboy_id, tipo, valor) "
            "VALUES (?,?,?,?)", (doc_id, motoboy_id, tipo_item, valor))
    return doc_id


def validar_documento(conn, doc_id, legivel, assinatura, valor_ok, status, obs, validado_por):
    """Registra a validação de um documento pelo financeiro (checklist + status).
    status: 'validado' | 'rejeitado' | 'pendente'. Guarda a autoria e o horário (BR)."""
    from datetime import datetime, timezone, timedelta
    agora = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE prestacao_documentos SET val_legivel=?, val_assinatura=?, val_valor=?, "
        "status=?, obs_validacao=?, validado_por=?, validado_em=? WHERE id=?",
        (1 if legivel else 0, 1 if assinatura else 0, 1 if valor_ok else 0,
         status, obs, validado_por, agora, doc_id))
    conn.commit()
