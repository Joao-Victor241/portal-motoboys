-- ============================================================================
-- Portal de Motoboys — modelo de dados (PostgreSQL)
-- O Postgres é a FONTE DA VERDADE. SIAC e DMP recebem cópias via adaptadores.
-- Rascunho v1 (Fase 1). Ajustar com a Gabriela conforme as regras finais.
-- ============================================================================

-- Lojas da operação (as 5 unidades).
CREATE TABLE IF NOT EXISTS lojas (
    id          SERIAL PRIMARY KEY,
    codigo      TEXT UNIQUE NOT NULL,        -- código usado no SIAC
    nome        TEXT NOT NULL,
    ativo       BOOLEAN NOT NULL DEFAULT TRUE
);

-- Operadores Logísticos (empresas terceiras que cadastram motoboys).
CREATE TABLE IF NOT EXISTS ols (
    id              SERIAL PRIMARY KEY,
    nome            TEXT NOT NULL,
    cnpj            TEXT UNIQUE,
    ativo           BOOLEAN NOT NULL DEFAULT TRUE,
    limite_global   INTEGER                   -- limite total de motoboys ativos (RF-03)
);

-- Limite de motoboys por OL EM CADA loja (a Gabriela pediu limite por OL/loja).
CREATE TABLE IF NOT EXISTS ol_loja_limite (
    ol_id       INTEGER NOT NULL REFERENCES ols(id),
    loja_id     INTEGER NOT NULL REFERENCES lojas(id),
    limite      INTEGER NOT NULL,
    PRIMARY KEY (ol_id, loja_id)
);

-- Usuários do portal (login + perfil).
CREATE TABLE IF NOT EXISTS usuarios (
    id          SERIAL PRIMARY KEY,
    login       TEXT UNIQUE NOT NULL,
    senha_hash  TEXT NOT NULL,                -- bcrypt; nunca senha em texto puro
    perfil      TEXT NOT NULL CHECK (perfil IN ('admin', 'ol', 'operador')),
    ol_id       INTEGER REFERENCES ols(id),   -- preenchido só para perfil 'ol'
    ativo       BOOLEAN NOT NULL DEFAULT TRUE
);

-- Motoboy = IDENTIDADE única por CPF/biometria (RF-05: pode estar em várias OLs
-- sem duplicar o registro biométrico).
CREATE TABLE IF NOT EXISTS motoboys (
    id                      SERIAL PRIMARY KEY,
    cpf                     TEXT UNIQUE NOT NULL,
    nome                    TEXT NOT NULL,
    nascimento              DATE,
    cnh                     TEXT,
    cnh_venc                DATE,
    telefone                TEXT,
    foto_path               TEXT,             -- caminho/URL da selfie
    bloqueado_permanente    BOOLEAN NOT NULL DEFAULT FALSE,  -- bloqueio cross-OL (RF-06)
    motivo_bloqueio         TEXT,
    dmp_person_id           INTEGER,          -- Id retornado pelo DMP
    criado_em               TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Cadastro = vínculo de um motoboy a uma OL numa loja, com prazo e tipo.
CREATE TABLE IF NOT EXISTS cadastros (
    id              SERIAL PRIMARY KEY,
    motoboy_id      INTEGER NOT NULL REFERENCES motoboys(id),
    ol_id           INTEGER NOT NULL REFERENCES ols(id),
    loja_id         INTEGER NOT NULL REFERENCES lojas(id),
    placa           TEXT,
    tipo            TEXT CHECK (tipo IN ('fixo', 'free')),
    situacao        TEXT NOT NULL DEFAULT 'ativo',   -- ativo / inativo / vencido
    valido_de       DATE,
    valido_ate      DATE,                            -- RF-04: vencido bloqueia acesso
    enviado_siac    BOOLEAN NOT NULL DEFAULT FALSE,
    enviado_dmp     BOOLEAN NOT NULL DEFAULT FALSE,
    criado_por      INTEGER REFERENCES usuarios(id),
    criado_em       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (motoboy_id, ol_id, loja_id)
);

-- Link de selfie enviado ao motoboy (token de uso único, com expiração).
CREATE TABLE IF NOT EXISTS selfie_links (
    token       TEXT PRIMARY KEY,
    motoboy_id  INTEGER NOT NULL REFERENCES motoboys(id),
    expira_em   TIMESTAMPTZ NOT NULL,
    usado_em    TIMESTAMPTZ
);

-- Eventos de entrada/saída lidos do DMP (alimenta fila FIFO e painéis).
CREATE TABLE IF NOT EXISTS acesso_eventos (
    id              BIGSERIAL PRIMARY KEY,
    motoboy_id      INTEGER REFERENCES motoboys(id),
    loja_id         INTEGER REFERENCES lojas(id),
    tipo            TEXT,                     -- entrada / saida
    ocorrido_em     TIMESTAMPTZ NOT NULL,
    dmp_pointer     BIGINT                    -- ponteiro do AccessLog do DMP
);

-- Trilha de auditoria (RF não funcional: quem fez o quê e quando).
CREATE TABLE IF NOT EXISTS auditoria (
    id          BIGSERIAL PRIMARY KEY,
    usuario_id  INTEGER REFERENCES usuarios(id),
    acao        TEXT NOT NULL,
    entidade    TEXT,
    entidade_id INTEGER,
    detalhe     JSONB,
    criado_em   TIMESTAMPTZ NOT NULL DEFAULT now()
);
