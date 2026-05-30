-- ================================================================
-- functions.sql — Execute no SQL Editor do Supabase após o schema.sql
-- ================================================================

CREATE OR REPLACE FUNCTION buscar_empresas_raio(
    lat_input          DOUBLE PRECISION,
    lon_input          DOUBLE PRECISION,
    raio_km            DOUBLE PRECISION,
    limite_resultados  INTEGER          DEFAULT 1000,
    cnae_input         TEXT             DEFAULT NULL
)
RETURNS TABLE (
    cnpj           TEXT,
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
    distancia_km   DOUBLE PRECISION
)
LANGUAGE sql
STABLE
AS $$
    SELECT
        e.cnpj,
        e.razao_social,
        e.nome_fantasia,
        e.cnae_principal,
        e.descricao_cnae,
        e.cidade,
        e.uf,
        e.bairro,
        e.logradouro,
        e.numero,
        e.cep,
        e.telefone,
        e.email,
        e.latitude,
        e.longitude,
        ROUND(
            (ST_Distance(
                e.geom,
                ST_SetSRID(ST_MakePoint(lon_input, lat_input), 4326)::geography
            ) / 1000.0)::numeric, 2
        )::double precision AS distancia_km
    FROM empresas e
    WHERE
        e.geom IS NOT NULL
        AND ST_DWithin(
            e.geom,
            ST_SetSRID(ST_MakePoint(lon_input, lat_input), 4326)::geography,
            raio_km * 1000.0
        )
        AND (cnae_input IS NULL OR e.cnae_principal ILIKE cnae_input || '%')
    ORDER BY distancia_km
    LIMIT limite_resultados;
$$;

-- Permitir chamada anônima via Supabase JS client
GRANT EXECUTE ON FUNCTION buscar_empresas_raio TO anon;
