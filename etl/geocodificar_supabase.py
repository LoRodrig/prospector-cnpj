import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta

import requests

try:
    import psycopg2
except ImportError:
    psycopg2 = None


BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = Path(os.getenv("LOCALAPPDATA", BASE_DIR)) / "prospector_cnpj"
CACHE_FILE = CACHE_DIR / "geocode_cache.json"


def agora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def formatar_duracao(segundos):
    return str(timedelta(seconds=int(segundos)))

CIDADES_PADRAO = [
    "MARINGA",
    "SARANDI",
    "PAICANDU",
    "MARIALVA",
    "MANDAGUARI",
]

CENTROS_CIDADE = {
    "MARINGA": (-23.4205, -51.9331),
    "SARANDI": (-23.4436, -51.8760),
    "PAICANDU": (-23.4575, -52.0486),
    "MARIALVA": (-23.4850, -51.7917),
    "MANDAGUARI": (-23.5478, -51.6708),
}

# Caixa aproximada da regiao de Maringa.
# Nominatim usa viewbox: esquerda, topo, direita, baixo.
REGIAO_VIEWBOX = "-52.25,-23.15,-51.65,-23.65"
LAT_MIN, LAT_MAX = -23.65, -23.15
LON_MIN, LON_MAX = -52.25, -51.65


def carregar_env(caminho=BASE_DIR / ".env"):
    if not caminho.exists():
        return
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, valor = linha.split("=", 1)
        os.environ.setdefault(chave.strip(), valor.strip().strip('"').strip("'"))


def conectar_supabase():
    if psycopg2 is None:
        raise RuntimeError("Instale a dependencia: pip install psycopg2-binary")

    carregar_env()

    database_url = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DATABASE_URL")
    if database_url:
        return psycopg2.connect(database_url, sslmode="require")

    host = os.getenv("SUPABASE_HOST")
    port = os.getenv("SUPABASE_PORT", "5432")
    dbname = os.getenv("SUPABASE_DB", "postgres")
    user = os.getenv("SUPABASE_USER", "postgres")
    password = os.getenv("SUPABASE_PASSWORD")
    project_ref = os.getenv("SUPABASE_PROJECT_REF")

    if "pooler.supabase.com" in (host or "") and "." not in user and project_ref:
        user = f"{user}.{project_ref}"

    if not host or not password:
        raise RuntimeError("Credenciais do Supabase ausentes no .env.")

    return psycopg2.connect(
        host=host,
        port=port,
        dbname=dbname,
        user=user,
        password=password,
        sslmode="require",
    )


def garantir_colunas_geocoding(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            ALTER TABLE empresas
            ADD COLUMN IF NOT EXISTS geocoding_nivel TEXT,
            ADD COLUMN IF NOT EXISTS geocoding_origem TEXT,
            ADD COLUMN IF NOT EXISTS geocoding_atualizado_em TIMESTAMPTZ
            """
        )
        cur.execute(
            """
            UPDATE empresas
            SET
                geocoding_nivel = 'legado',
                geocoding_origem = 'anterior',
                geocoding_atualizado_em = coalesce(geocoding_atualizado_em, now())
            WHERE geom IS NOT NULL
              AND latitude IS NOT NULL
              AND longitude IS NOT NULL
              AND geocoding_nivel IS NULL
            """
        )
    conn.commit()


def normalizar(valor):
    return re.sub(r"\s+", " ", str(valor or "").strip().upper())


def somente_digitos(valor):
    return re.sub(r"\D", "", str(valor or ""))


def carregar_cache():
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def salvar_cache(cache):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def dentro_da_regiao(lat, lon):
    return LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX


def montar_consultas(row):
    _, _, cidade, uf, bairro, logradouro, numero, cep = row
    cidade = normalizar(cidade)
    uf = normalizar(uf)
    bairro = normalizar(bairro)
    logradouro = normalizar(logradouro)
    numero = normalizar(numero)
    cep = somente_digitos(cep)

    consultas = []

    if logradouro and numero:
        consultas.append(
            {
                "street": f"{numero} {logradouro}",
                "city": cidade,
                "state": uf,
                "country": "Brasil",
                "postalcode": cep or None,
            }
        )
    if logradouro:
        consultas.append(
            {
                "street": logradouro,
                "city": cidade,
                "state": uf,
                "country": "Brasil",
                "postalcode": cep or None,
            }
        )

    texto_partes = [
        logradouro,
        numero,
        bairro,
        cidade,
        uf,
        cep,
        "Brasil",
    ]
    consultas.append({"q": ", ".join(parte for parte in texto_partes if parte)})

    if cep:
        consultas.append({"q": f"{cep}, {cidade}, {uf}, Brasil"})

    return consultas


def montar_endereco_texto(consulta):
    if consulta.get("q"):
        return consulta["q"]
    partes = [
        consulta.get("street"),
        consulta.get("city"),
        consulta.get("state"),
        consulta.get("postalcode"),
        consulta.get("country"),
    ]
    return ", ".join(parte for parte in partes if parte)


def geocodificar_nominatim(consulta, user_agent):
    params = {
        "format": "json",
        "limit": "5",
        "countrycodes": "br",
        "addressdetails": "1",
        "viewbox": REGIAO_VIEWBOX,
        "bounded": "1",
    }
    params.update({k: v for k, v in consulta.items() if v})

    response = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params=params,
        headers={"User-Agent": user_agent},
        timeout=30,
    )
    response.raise_for_status()

    for item in response.json():
        lat = float(item["lat"])
        lon = float(item["lon"])
        if dentro_da_regiao(lat, lon):
            return {
                "latitude": lat,
                "longitude": lon,
                "display_name": item.get("display_name", ""),
            }

    return None


def geocodificar_google(consulta, api_key):
    endereco = montar_endereco_texto(consulta)
    if not endereco:
        return None

    response = requests.get(
        "https://maps.googleapis.com/maps/api/geocode/json",
        params={
            "address": endereco,
            "key": api_key,
            "region": "br",
            "components": "country:BR",
            "bounds": f"{LAT_MIN},{LON_MIN}|{LAT_MAX},{LON_MAX}",
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    status = data.get("status")

    if status == "ZERO_RESULTS":
        return None
    if status != "OK":
        mensagem = data.get("error_message") or status or "erro desconhecido"
        raise RuntimeError(f"Google Geocoding: {mensagem}")

    for item in data.get("results", []):
        location = item.get("geometry", {}).get("location", {})
        if "lat" not in location or "lng" not in location:
            continue
        lat = float(location["lat"])
        lon = float(location["lng"])
        if dentro_da_regiao(lat, lon):
            return {
                "latitude": lat,
                "longitude": lon,
                "display_name": item.get("formatted_address", ""),
                "location_type": item.get("geometry", {}).get("location_type", ""),
            }

    return None


def geocodificar(consulta, provider, user_agent, google_api_key):
    if provider == "google":
        return geocodificar_google(consulta, google_api_key)
    return geocodificar_nominatim(consulta, user_agent)


def cep_tem_geocoding(conn, cep_norm, cidade):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM empresas
            WHERE cidade = %s
              AND regexp_replace(coalesce(cep, ''), '\\D', '', 'g') = %s
              AND geom IS NOT NULL
              AND latitude IS NOT NULL
              AND longitude IS NOT NULL
            LIMIT 1
            """,
            (cidade, cep_norm),
        )
        return cur.fetchone() is not None


def buscar_ceps_pendentes(conn, cidades, ignorar_ceps=None, limite_ceps=500, scan_limit=20000):
    """
    Retorna CEPs únicos pendentes sem agrupar a tabela inteira.
    Faz uma varredura leve por id e filtra os CEPs em Python para evitar timeout.
    """
    ignorar_ceps = ignorar_ceps or set()
    candidatos = {}

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                regexp_replace(coalesce(cep, ''), '\\D', '', 'g') AS cep_norm,
                cidade,
                uf,
                logradouro,
                bairro
            FROM empresas
            WHERE cidade = ANY(%s)
              AND regexp_replace(coalesce(cep, ''), '\\D', '', 'g') <> ''
              AND (latitude IS NULL OR longitude IS NULL OR geom IS NULL)
            ORDER BY id
            LIMIT %s
            """,
            (cidades, scan_limit),
        )

        for cep_norm, cidade, uf, logradouro, bairro in cur.fetchall():
            chave = (cidade, cep_norm)
            if chave in ignorar_ceps or chave in candidatos:
                continue
            if cep_tem_geocoding(conn, cep_norm, cidade):
                continue
            candidatos[chave] = (cep_norm, cidade, uf, logradouro, bairro)
            if len(candidatos) >= limite_ceps:
                break

    return list(candidatos.values())


def propagar_cep_geocodificado(conn, cep_norm, cidade, lat, lon):
    """
    Após geocodificar um CEP, propaga para TODAS as empresas daquele CEP de uma vez.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE empresas
            SET
                latitude  = %s,
                longitude = %s,
                geom      = ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                geocoding_nivel          = 'cep',
                geocoding_origem         = 'nominatim_cep',
                geocoding_atualizado_em  = now()
            WHERE regexp_replace(coalesce(cep, ''), '\\D', '', 'g') = %s
              AND cidade = %s
              AND (latitude IS NULL OR longitude IS NULL OR geom IS NULL)
            """,
            (lat, lon, lon, lat, cep_norm, cidade),
        )
        atualizados = cur.rowcount
    conn.commit()
    return atualizados


def geocodificar_por_ceps(conn, cidades, user_agent, provider, google_api_key,
                          delay, cache, verbose=True):
    """
    Estratégia principal: geocodifica 1 req por CEP único e propaga em batch.
    140k empresas com ~2000 CEPs = ~35 minutos em vez de ~48 horas.
    """
    ceps = buscar_ceps_pendentes(conn, cidades)
    if not ceps:
        if verbose:
            print("Nenhum CEP pendente encontrado.")
        return 0

    total_ceps     = len(ceps)
    total_empresas = 0
    geocodificados = 0
    inicio         = time.monotonic()

    if verbose:
        print(f"\nCEPs únicos pendentes: {total_ceps}")
        print(f"Estimativa: ~{total_ceps * delay / 60:.0f} minutos de API\n")

    for i, (cep_norm, cidade, uf, logradouro, bairro) in enumerate(ceps, 1):
        # Monta consulta: tenta CEP puro primeiro (mais preciso), depois endereço
        consultas = [
            {"q": f"{cep_norm[:5]}-{cep_norm[5:]}, {cidade}, {uf}, Brasil"},
            {"q": f"{cep_norm}, {cidade}, {uf}, Brasil"},
        ]
        if logradouro:
            consultas.insert(0, {
                "street": logradouro,
                "city":   cidade,
                "state":  uf,
                "country": "Brasil",
                "postalcode": cep_norm,
            })

        resultado = None
        for consulta in consultas:
            chave = normalizar(json.dumps(
                {"provider": provider, "consulta": consulta},
                sort_keys=True, ensure_ascii=False,
            ))

            if chave in cache:
                resultado = cache[chave]
                if resultado:
                    break
                continue

            try:
                resultado = geocodificar(consulta, provider, user_agent, google_api_key)
            except (requests.RequestException, RuntimeError) as erro:
                print(f"  Falha CEP {cep_norm}: {erro}")
                resultado = None

            cache[chave] = resultado
            time.sleep(delay)

            if resultado:
                break

        if resultado:
            n = propagar_cep_geocodificado(
                conn, cep_norm, cidade,
                resultado["latitude"], resultado["longitude"]
            )
            total_empresas += n
            geocodificados += 1

        # Salva cache a cada 50 CEPs
        if i % 50 == 0:
            salvar_cache(cache)

        if verbose and i % 25 == 0:
            decorrido = time.monotonic() - inicio
            media = decorrido / i
            restante = (total_ceps - i) * media
            print(
                f"  CEPs: {i}/{total_ceps} | "
                f"Geocodificados: {geocodificados} | "
                f"Empresas preenchidas: {total_empresas} | "
                f"Decorrido: {formatar_duracao(decorrido)} | "
                f"Restante estimado: {formatar_duracao(restante)}"
            )

    salvar_cache(cache)
    if verbose:
        print(
            f"\nGeocodificação por CEP concluída: "
            f"{geocodificados}/{total_ceps} CEPs resolvidos | "
            f"{total_empresas} empresas preenchidas | "
            f"Duração: {formatar_duracao(time.monotonic() - inicio)}"
        )
    return total_empresas


def buscar_pendentes(conn, limite, cidades, ignorar_ids, modo_amostra="sequencial"):
    with conn.cursor() as cur:
        if modo_amostra == "cep":
            cur.execute(
                """
                WITH base AS (
                    SELECT
                        id,
                        cnpj,
                        cidade,
                        uf,
                        bairro,
                        logradouro,
                        numero,
                        cep,
                        row_number() OVER (
                            PARTITION BY cidade, regexp_replace(coalesce(cep, ''), '\\D', '', 'g')
                            ORDER BY
                                CASE WHEN coalesce(logradouro, '') <> '' AND coalesce(numero, '') <> '' THEN 0 ELSE 1 END,
                                id
                        ) AS rn
                    FROM empresas
                    WHERE (latitude IS NULL OR longitude IS NULL OR geom IS NULL)
                      AND cidade = ANY(%s)
                      AND NOT (id = ANY(%s))
                      AND regexp_replace(coalesce(cep, ''), '\\D', '', 'g') <> ''
                )
                SELECT id, cnpj, cidade, uf, bairro, logradouro, numero, cep
                FROM base
                WHERE rn = 1
                ORDER BY
                    array_position(%s::text[], cidade),
                    regexp_replace(coalesce(cep, ''), '\\D', '', 'g'),
                    id
                LIMIT %s
                """,
                (cidades, list(ignorar_ids), cidades, limite if limite else 2147483647),
            )
            return cur.fetchall()

        if modo_amostra == "cep_popular":
            cur.execute(
                """
                WITH base AS (
                    SELECT
                        id,
                        cnpj,
                        cidade,
                        uf,
                        bairro,
                        logradouro,
                        numero,
                        cep,
                        regexp_replace(coalesce(cep, ''), '\\D', '', 'g') AS cep_norm,
                        count(*) OVER (
                            PARTITION BY cidade, regexp_replace(coalesce(cep, ''), '\\D', '', 'g')
                        ) AS qtd_cep,
                        row_number() OVER (
                            PARTITION BY cidade, regexp_replace(coalesce(cep, ''), '\\D', '', 'g')
                            ORDER BY
                                CASE WHEN coalesce(logradouro, '') <> '' AND coalesce(numero, '') <> '' THEN 0 ELSE 1 END,
                                id
                        ) AS rn
                    FROM empresas
                    WHERE (latitude IS NULL OR longitude IS NULL OR geom IS NULL)
                      AND cidade = ANY(%s)
                      AND NOT (id = ANY(%s))
                      AND regexp_replace(coalesce(cep, ''), '\\D', '', 'g') <> ''
                )
                SELECT id, cnpj, cidade, uf, bairro, logradouro, numero, cep
                FROM base
                WHERE rn = 1
                ORDER BY
                    qtd_cep DESC,
                    array_position(%s::text[], cidade),
                    cep_norm,
                    id
                LIMIT %s
                """,
                (cidades, list(ignorar_ids), cidades, limite if limite else 2147483647),
            )
            return cur.fetchall()

        if modo_amostra == "bairro":
            cur.execute(
                """
                WITH base AS (
                    SELECT
                        id,
                        cnpj,
                        cidade,
                        uf,
                        bairro,
                        logradouro,
                        numero,
                        cep,
                        row_number() OVER (
                            PARTITION BY cidade, upper(trim(coalesce(bairro, '')))
                            ORDER BY
                                CASE WHEN coalesce(logradouro, '') <> '' AND coalesce(numero, '') <> '' THEN 0 ELSE 1 END,
                                id
                        ) AS rn
                    FROM empresas
                    WHERE (latitude IS NULL OR longitude IS NULL OR geom IS NULL)
                      AND cidade = ANY(%s)
                      AND NOT (id = ANY(%s))
                      AND coalesce(bairro, '') <> ''
                )
                SELECT id, cnpj, cidade, uf, bairro, logradouro, numero, cep
                FROM base
                WHERE rn = 1
                ORDER BY
                    array_position(%s::text[], cidade),
                    upper(trim(coalesce(bairro, ''))),
                    id
                LIMIT %s
                """,
                (cidades, list(ignorar_ids), cidades, limite if limite else 2147483647),
            )
            return cur.fetchall()

        if modo_amostra == "bairro_popular":
            cur.execute(
                """
                WITH base AS (
                    SELECT
                        id,
                        cnpj,
                        cidade,
                        uf,
                        bairro,
                        logradouro,
                        numero,
                        cep,
                        upper(trim(coalesce(bairro, ''))) AS bairro_norm,
                        count(*) OVER (
                            PARTITION BY cidade, upper(trim(coalesce(bairro, '')))
                        ) AS qtd_bairro,
                        row_number() OVER (
                            PARTITION BY cidade, upper(trim(coalesce(bairro, '')))
                            ORDER BY
                                CASE WHEN coalesce(logradouro, '') <> '' AND coalesce(numero, '') <> '' THEN 0 ELSE 1 END,
                                id
                        ) AS rn
                    FROM empresas
                    WHERE (latitude IS NULL OR longitude IS NULL OR geom IS NULL)
                      AND cidade = ANY(%s)
                      AND NOT (id = ANY(%s))
                      AND coalesce(bairro, '') <> ''
                )
                SELECT id, cnpj, cidade, uf, bairro, logradouro, numero, cep
                FROM base
                WHERE rn = 1
                ORDER BY
                    qtd_bairro DESC,
                    array_position(%s::text[], cidade),
                    bairro_norm,
                    id
                LIMIT %s
                """,
                (cidades, list(ignorar_ids), cidades, limite if limite else 2147483647),
            )
            return cur.fetchall()

        cur.execute(
            """
            SELECT
                id,
                cnpj,
                cidade,
                uf,
                bairro,
                logradouro,
                numero,
                cep
            FROM empresas
            WHERE (latitude IS NULL OR longitude IS NULL OR geom IS NULL)
              AND cidade = ANY(%s)
              AND NOT (id = ANY(%s))
            ORDER BY
                array_position(%s::text[], cidade),
                bairro,
                logradouro,
                numero,
                id
            LIMIT %s
            """,
            (cidades, list(ignorar_ids), cidades, limite if limite else 2147483647),
        )
        return cur.fetchall()


def preencher_enderecos_iguais(conn, cidades, tamanho_lote=250):
    total_atualizados = 0
    with conn.cursor() as cur:
        while True:
            try:
                cur.execute(
                    """
                    SET LOCAL statement_timeout = '30s';
                    WITH pendentes AS (
                        SELECT id, cidade, uf, cep, logradouro, numero
                        FROM empresas
                        WHERE (latitude IS NULL OR longitude IS NULL OR geom IS NULL)
                          AND cidade = ANY(%s)
                          AND coalesce(logradouro, '') <> ''
                          AND coalesce(numero, '') <> ''
                        ORDER BY id
                        LIMIT %s
                    ),
                    origem AS (
                        SELECT
                            destino.id,
                            min(fonte.latitude) AS latitude,
                            min(fonte.longitude) AS longitude
                        FROM pendentes destino
                        JOIN empresas fonte
                          ON fonte.cidade = destino.cidade
                         AND fonte.uf = destino.uf
                         AND regexp_replace(coalesce(fonte.cep, ''), '\\D', '', 'g') =
                             regexp_replace(coalesce(destino.cep, ''), '\\D', '', 'g')
                         AND upper(trim(coalesce(fonte.logradouro, ''))) =
                             upper(trim(coalesce(destino.logradouro, '')))
                         AND upper(trim(coalesce(fonte.numero, ''))) =
                             upper(trim(coalesce(destino.numero, '')))
                         AND fonte.latitude IS NOT NULL
                         AND fonte.longitude IS NOT NULL
                         AND fonte.geom IS NOT NULL
                        GROUP BY destino.id
                    )
                    UPDATE empresas destino
                    SET
                        latitude = origem.latitude,
                        longitude = origem.longitude,
                        geom = ST_SetSRID(
                            ST_MakePoint(origem.longitude, origem.latitude),
                            4326
                        )::geography,
                        geocoding_nivel = 'mesmo_endereco',
                        geocoding_origem = 'banco',
                        geocoding_atualizado_em = now()
                    FROM origem
                    WHERE destino.id = origem.id
                    """,
                    (cidades, tamanho_lote),
                )
            except psycopg2.Error as erro:
                conn.rollback()
                print(f"  Endereco identico interrompido por timeout/erro: {erro.pgerror or erro}")
                break
            atualizados = cur.rowcount
            conn.commit()
            total_atualizados += atualizados
            if atualizados:
                print(f"  Copiados por endereco identico: +{atualizados} (total {total_atualizados})")
            if atualizados == 0:
                break
    return total_atualizados


def preencher_por_cep(conn, cidades):
    total_atualizados = 0
    with conn.cursor() as cur:
        while True:
            cur.execute(
                """
                SET LOCAL statement_timeout = '45s';
                WITH pendentes AS (
                    SELECT
                        id,
                        cidade,
                        uf,
                        regexp_replace(coalesce(cep, ''), '\\D', '', 'g') AS cep_norm
                    FROM empresas
                    WHERE (latitude IS NULL OR longitude IS NULL OR geom IS NULL)
                      AND cidade = ANY(%s)
                      AND regexp_replace(coalesce(cep, ''), '\\D', '', 'g') <> ''
                    ORDER BY id
                    LIMIT 2000
                ),
                origem AS (
                    SELECT
                        p.id,
                        avg(e.latitude) AS latitude,
                        avg(e.longitude) AS longitude,
                        count(*) AS qtd
                    FROM pendentes p
                    JOIN empresas e
                      ON e.cidade = p.cidade
                     AND e.uf = p.uf
                     AND regexp_replace(coalesce(e.cep, ''), '\\D', '', 'g') = p.cep_norm
                     AND e.latitude IS NOT NULL
                     AND e.longitude IS NOT NULL
                     AND e.geom IS NOT NULL
                    GROUP BY p.id
                    HAVING count(*) >= 2
                )
                UPDATE empresas destino
                SET
                    latitude = origem.latitude,
                    longitude = origem.longitude,
                    geom = ST_SetSRID(
                        ST_MakePoint(origem.longitude, origem.latitude),
                        4326
                    )::geography,
                    geocoding_nivel = 'cep',
                    geocoding_origem = 'banco',
                    geocoding_atualizado_em = now()
                FROM origem
                WHERE destino.id = origem.id
                """,
                (cidades,),
            )
            atualizados = cur.rowcount
            conn.commit()
            total_atualizados += atualizados
            if atualizados:
                print(f"  Copiados por CEP: +{atualizados} (total {total_atualizados})")
            if atualizados == 0:
                break
    return total_atualizados


def preencher_por_bairro(conn, cidades, tamanho_lote=500):
    total_atualizados = 0
    with conn.cursor() as cur:
        while True:
            try:
                cur.execute(
                    """
                    SET LOCAL statement_timeout = '30s';
                    WITH pendentes AS (
                        SELECT id, cidade, uf, bairro
                        FROM empresas
                        WHERE (latitude IS NULL OR longitude IS NULL OR geom IS NULL)
                          AND cidade = ANY(%s)
                          AND coalesce(bairro, '') <> ''
                        ORDER BY id
                        LIMIT %s
                    ),
                    origem AS (
                        SELECT
                            destino.id,
                            avg(fonte.latitude) AS latitude,
                            avg(fonte.longitude) AS longitude,
                            count(*) AS qtd
                        FROM pendentes destino
                        JOIN empresas fonte
                          ON fonte.cidade = destino.cidade
                         AND fonte.uf = destino.uf
                         AND upper(trim(coalesce(fonte.bairro, ''))) =
                             upper(trim(coalesce(destino.bairro, '')))
                         AND fonte.latitude IS NOT NULL
                         AND fonte.longitude IS NOT NULL
                         AND fonte.geom IS NOT NULL
                        GROUP BY destino.id
                        HAVING count(*) >= 3
                    )
                    UPDATE empresas destino
                    SET
                        latitude = origem.latitude,
                        longitude = origem.longitude,
                        geom = ST_SetSRID(
                            ST_MakePoint(origem.longitude, origem.latitude),
                            4326
                        )::geography,
                        geocoding_nivel = 'bairro',
                        geocoding_origem = 'banco',
                        geocoding_atualizado_em = now()
                    FROM origem
                    WHERE destino.id = origem.id
                    """,
                    (cidades, tamanho_lote),
                )
            except psycopg2.Error as erro:
                conn.rollback()
                print(f"  Bairro interrompido por timeout/erro: {erro.pgerror or erro}")
                break
            atualizados = cur.rowcount
            conn.commit()
            total_atualizados += atualizados
            if atualizados:
                print(f"  Copiados por bairro: +{atualizados} (total {total_atualizados})")
            if atualizados == 0:
                break
    return total_atualizados


def preencher_por_centro_cidade(conn, cidades):
    total_atualizados = 0
    with conn.cursor() as cur:
        for cidade in cidades:
            if cidade not in CENTROS_CIDADE:
                continue
            lat, lon = CENTROS_CIDADE[cidade]
            while True:
                cur.execute(
                    """
                    SET LOCAL statement_timeout = '45s';
                    WITH pendentes AS (
                        SELECT id
                        FROM empresas
                        WHERE (latitude IS NULL OR longitude IS NULL OR geom IS NULL)
                          AND cidade = %s
                        ORDER BY id
                        LIMIT 5000
                    )
                    UPDATE empresas destino
                    SET
                        latitude = %s,
                        longitude = %s,
                        geom = ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                        geocoding_nivel = 'cidade',
                        geocoding_origem = 'banco',
                        geocoding_atualizado_em = now()
                    FROM pendentes
                    WHERE destino.id = pendentes.id
                    """,
                    (cidade, lat, lon, lon, lat),
                )
                atualizados = cur.rowcount
                conn.commit()
                total_atualizados += atualizados
                if atualizados:
                    print(f"  Centro de {cidade}: +{atualizados} (total {total_atualizados})")
                if atualizados == 0:
                    break
    return total_atualizados


def atualizar_coordenada(conn, empresa_id, lat, lon, nivel, origem):
    with conn.cursor() as cur:
        cur.execute(
            """
            SET LOCAL statement_timeout = '60s';
            UPDATE empresas
            SET
                latitude = %s,
                longitude = %s,
                geom = ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                geocoding_nivel = %s,
                geocoding_origem = %s,
                geocoding_atualizado_em = now()
            WHERE id = %s
            """,
            (lat, lon, lon, lat, nivel, origem, empresa_id),
        )


def tentar_atualizar_coordenada(conn, empresa_id, cnpj, lat, lon, nivel, origem):
    try:
        atualizar_coordenada(conn, empresa_id, lat, lon, nivel, origem)
        conn.commit()
        return True
    except psycopg2.Error as erro:
        conn.rollback()
        print(f"Update pulado CNPJ {cnpj}: {erro.pgerror or erro}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Preenche latitude/longitude/geom da tabela empresas no Supabase."
    )
    parser.add_argument("--limite", type=int, default=500)
    parser.add_argument("--lote", type=int, default=100)
    parser.add_argument("--max-lotes", type=int, default=0)
    parser.add_argument("--delay", type=float, default=1.1)
    parser.add_argument("--pausa-lote", type=float, default=3.0)
    parser.add_argument("--cidade", action="append", help="Cidade alvo. Pode repetir.")
    parser.add_argument(
        "--amostra",
        choices=["sequencial", "cep", "cep_popular", "bairro", "bairro_popular"],
        default="sequencial",
        help="Como escolher pendentes para API: sequencial, cep, cep_popular, bairro ou bairro_popular.",
    )
    parser.add_argument(
        "--propagar-apos-lote",
        action="store_true",
        help="Depois de cada lote da API, propaga coordenadas por CEP e bairro.",
    )
    parser.add_argument(
        "--provider",
        choices=["nominatim", "google"],
        default=os.getenv("GEOCODER_PROVIDER", "nominatim").lower(),
        help="Servico de geocodificacao: nominatim ou google.",
    )
    parser.add_argument(
        "--sem-preencher-iguais",
        action="store_true",
        help="Nao copia coordenadas de enderecos identicos ja geocodificados.",
    )
    parser.add_argument(
        "--preencher-cep",
        action="store_true",
        help="Preenche pendentes pelo centro medio do CEP ja geocodificado (mais rapido, menos preciso).",
    )
    parser.add_argument(
        "--preencher-bairro",
        action="store_true",
        help="Preenche pendentes pela media do bairro ja geocodificado (rapido, aproximado).",
    )
    parser.add_argument(
        "--preencher-cidade",
        action="store_true",
        help="Preenche todos os pendentes restantes pelo centro da cidade (muito rapido, aproximado).",
    )
    parser.add_argument(
        "--somente-preenchimento",
        action="store_true",
        help="Executa apenas os preenchimentos SQL e nao chama API de geocodificacao.",
    )
    parser.add_argument(
        "--modo-cep",
        action="store_true",
        help=(
            "RECOMENDADO: geocodifica 1 requisicao por CEP unico e propaga para todas "
            "as empresas daquele CEP em batch. ~2000 CEPs = ~35 min no lugar de ~48h."
        ),
    )
    args = parser.parse_args()

    email = os.getenv("NOMINATIM_EMAIL", "rodri-local")
    user_agent = f"prospector-cnpj-geocoder/1.0 ({email})"
    google_api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if args.provider == "google" and not google_api_key:
        raise RuntimeError("Para usar --provider google, defina GOOGLE_MAPS_API_KEY no .env.")
    cidades = [normalizar(c) for c in (args.cidade or CIDADES_PADRAO)]

    conn = conectar_supabase()
    garantir_colunas_geocoding(conn)
    cache = carregar_cache()
    atualizados = 0
    tentativas = 0
    processados = 0
    lote_atual = 0
    ids_tentados = set()
    inicio = time.monotonic()
    inicio_relogio = agora()

    print(f"Inicio: {inicio_relogio}")
    print(f"Provider geocodificacao: {args.provider}")
    if not args.sem_preencher_iguais:
        print("Preenchendo enderecos identicos ja geocodificados...")
        inicio_preenchimento = time.monotonic()
        copiados = preencher_enderecos_iguais(conn, cidades)
        print(
            f"Enderecos identicos preenchidos: {copiados} | "
            f"Duracao: {formatar_duracao(time.monotonic() - inicio_preenchimento)}"
        )

    if args.preencher_cep:
        print("Preenchendo pelo centro medio do CEP ja geocodificado...")
        inicio_cep = time.monotonic()
        try:
            copiados_cep = preencher_por_cep(conn, cidades)
        except psycopg2.Error as erro:
            conn.rollback()
            copiados_cep = 0
            print(f"Preenchimento por CEP pulado por timeout/erro: {erro.pgerror or erro}")
        print(
            f"Registros preenchidos por CEP: {copiados_cep} | "
            f"Duracao: {formatar_duracao(time.monotonic() - inicio_cep)}"
        )

    if args.preencher_bairro:
        print("Preenchendo pelo centro medio do bairro ja geocodificado...")
        inicio_bairro = time.monotonic()
        copiados_bairro = preencher_por_bairro(conn, cidades)
        print(
            f"Registros preenchidos por bairro: {copiados_bairro} | "
            f"Duracao: {formatar_duracao(time.monotonic() - inicio_bairro)}"
        )

    if args.preencher_cidade:
        print("Preenchendo pendentes restantes pelo centro da cidade...")
        inicio_cidade = time.monotonic()
        try:
            copiados_cidade = preencher_por_centro_cidade(conn, cidades)
        except psycopg2.Error as erro:
            conn.rollback()
            copiados_cidade = 0
            print(f"Preenchimento por cidade pulado por timeout/erro: {erro.pgerror or erro}")
        print(
            f"Registros preenchidos por cidade: {copiados_cidade} | "
            f"Duracao: {formatar_duracao(time.monotonic() - inicio_cidade)}"
        )


    # ── MODO CEP: estrategia rapida recomendada ──────────────────────────────
    if args.modo_cep:
        print("\n=== MODO CEP: geocodificando por CEP unico + propagacao em batch ===")
        geocodificar_por_ceps(
            conn, cidades, user_agent, args.provider, google_api_key,
            args.delay, cache,
        )
        print("\nPropagando por bairro apos geocodificacao de CEPs...")
        preencher_por_bairro(conn, cidades)
        print("Propagando por centro de cidade para registros restantes...")
        preencher_por_centro_cidade(conn, cidades)
        salvar_cache(cache)
        conn.close()
        print(
            f"\nConcluido. Inicio: {inicio_relogio} | Fim: {agora()} | "
            f"Duracao total: {formatar_duracao(time.monotonic() - inicio)}"
        )
        return

    if args.somente_preenchimento:
        salvar_cache(cache)
        conn.close()
        print(
            f"Finalizado sem chamar API. Inicio: {inicio_relogio} | Fim: {agora()} | "
            f"Duracao total: {formatar_duracao(time.monotonic() - inicio)}"
        )
        return

    try:
        while True:
            if args.limite and processados >= args.limite:
                break
            if args.max_lotes and lote_atual >= args.max_lotes:
                break

            tamanho_lote = args.lote
            if args.limite:
                tamanho_lote = min(tamanho_lote, args.limite - processados)

            rows = buscar_pendentes(conn, tamanho_lote, cidades, ids_tentados, args.amostra)
            if not rows:
                print("Nenhum pendente encontrado.")
                break

            lote_atual += 1
            inicio_lote = time.monotonic()
            print(f"\n[{agora()}] Lote {lote_atual}: {len(rows)} pendentes")

            for row in rows:
                empresa_id, cnpj, cidade, uf, bairro, logradouro, numero, cep = row
                ids_tentados.add(empresa_id)
                consultas = montar_consultas(row)
                resultado = None

                for consulta in consultas:
                    chave = normalizar(
                        json.dumps(
                            {"provider": args.provider, "consulta": consulta},
                            sort_keys=True,
                            ensure_ascii=False,
                        )
                    )
                    if chave in cache:
                        resultado = cache[chave]
                    else:
                        try:
                            resultado = geocodificar(
                                consulta,
                                args.provider,
                                user_agent,
                                google_api_key,
                            )
                        except (requests.RequestException, RuntimeError) as erro:
                            print(f"Falha {args.provider} CNPJ {cnpj}: {erro}")
                            resultado = None
                        cache[chave] = resultado
                        salvar_cache(cache)
                        time.sleep(args.delay)

                    tentativas += 1
                    if resultado:
                        ok = tentar_atualizar_coordenada(
                            conn,
                            empresa_id,
                            cnpj,
                            resultado["latitude"],
                            resultado["longitude"],
                            "exato",
                            args.provider,
                        )
                        if ok:
                            atualizados += 1
                        break

                processados += 1
                if processados % 25 == 0:
                    decorrido = time.monotonic() - inicio
                    media = decorrido / processados if processados else 0
                    print(
                        f"Processados: {processados} | "
                        f"Tentativas: {tentativas} | Atualizados: {atualizados} | "
                        f"Decorrido: {formatar_duracao(decorrido)} | "
                        f"Media: {media:.1f}s/reg"
                    )

            salvar_cache(cache)
            duracao_lote = time.monotonic() - inicio_lote
            print(
                f"[{agora()}] Fim do lote {lote_atual} | "
                f"Duracao lote: {formatar_duracao(duracao_lote)} | "
                f"Total decorrido: {formatar_duracao(time.monotonic() - inicio)}"
            )
            if args.propagar_apos_lote:
                inicio_prop = time.monotonic()
                print("Propagando sementes do lote por CEP/bairro...")
                prop_cep = preencher_por_cep(conn, cidades)
                prop_bairro = preencher_por_bairro(conn, cidades)
                print(
                    f"Propagacao do lote: CEP +{prop_cep} | Bairro +{prop_bairro} | "
                    f"Duracao: {formatar_duracao(time.monotonic() - inicio_prop)}"
                )
            if args.pausa_lote:
                time.sleep(args.pausa_lote)

        salvar_cache(cache)
        duracao_total = time.monotonic() - inicio
        print(
            f"Finalizado. Lotes: {lote_atual} | Processados: {processados} | "
            f"Tentativas: {tentativas} | Atualizados: {atualizados} | "
            f"Inicio: {inicio_relogio} | Fim: {agora()} | "
            f"Duracao total: {formatar_duracao(duracao_total)}"
        )

    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as erro:
        print(f"ERRO: {erro}")
        sys.exit(1)
