-- ================================================================
-- schema.sql — Execute no SQL Editor do Supabase
-- ================================================================

-- 1. Habilitar PostGIS
CREATE EXTENSION IF NOT EXISTS postgis;

-- 2. Tabela principal
CREATE TABLE IF NOT EXISTS empresas (
    id             BIGSERIAL PRIMARY KEY,
    cnpj           TEXT        UNIQUE NOT NULL,
    razao_social   TEXT,
    nome_fantasia  TEXT,
    cnae_principal TEXT,
    descricao_cnae TEXT,
    cidade         TEXT,
    uf             CHAR(2),
    bairro         TEXT,
    logradouro     TEXT,
    numero         TEXT,
    cep            TEXT,
    telefone       TEXT,
    email          TEXT,
    latitude       DOUBLE PRECISION,
    longitude      DOUBLE PRECISION,
    geom           GEOGRAPHY(POINT, 4326)
);

-- 3. Índice espacial (essencial para ST_DWithin ser rápido)
CREATE INDEX IF NOT EXISTS idx_empresas_geom
    ON empresas USING GIST (geom);

-- 4. Índices de suporte para filtros comuns
CREATE INDEX IF NOT EXISTS idx_empresas_cidade    ON empresas (cidade);
CREATE INDEX IF NOT EXISTS idx_empresas_uf        ON empresas (uf);
CREATE INDEX IF NOT EXISTS idx_empresas_cnae      ON empresas (cnae_principal);

-- 5. Comentários
COMMENT ON TABLE  empresas              IS 'Empresas ativas da Receita Federal com geolocalização';
COMMENT ON COLUMN empresas.geom         IS 'Ponto geográfico WGS84 (PostGIS) — populado pelo geocodificar_supabase.py';
COMMENT ON COLUMN empresas.cnae_principal IS 'Código CNAE principal (7 dígitos, sem pontuação)';
