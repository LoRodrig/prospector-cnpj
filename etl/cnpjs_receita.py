import argparse
import csv
import gzip
import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path
from urllib.parse import quote, unquote

import requests
from xml.etree import ElementTree as ET

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None


RF_SHARE_TOKEN = "YggdBLfdninEJX9"
RF_HOST = "https://arquivos.receitafederal.gov.br"
RF_SHARE = f"{RF_HOST}/index.php/s/{RF_SHARE_TOKEN}"
WEBDAV_URL = f"{RF_HOST}/public.php/webdav/"
WEBDAV_PROPFIND = """<?xml version="1.0" encoding="utf-8"?>
<propfind xmlns="DAV:">
  <prop><displayname/><getlastmodified/><resourcetype/></prop>
</propfind>"""

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; cnpj-receita-etl/1.0)"
}

REQUEST_TIMEOUT = (10, 60)

BASE_DIR = Path(__file__).resolve().parent
PASTA_DOWNLOAD = BASE_DIR / "dados_receita"
PASTA_SAIDA = BASE_DIR / "database"
ARQUIVO_SAIDA = PASTA_SAIDA / "sample_data.csv.gz"
ARQUIVO_LINKS = BASE_DIR / "links_download_cnpj.txt"
CACHE_DIR = Path(os.getenv("LOCALAPPDATA", BASE_DIR)) / "prospector_cnpj"
ARQUIVO_GEOCODE_CACHE = CACHE_DIR / "geocode_cache.json"

CIDADES = {
    "MARINGA",
    "SARANDI",
    "PAICANDU",
    "MARIALVA",
    "MANDAGUARI",
}

COLUNAS_SAIDA = [
    "cnpj",
    "razao_social",
    "nome_fantasia",
    "cnae_principal",
    "descricao_cnae",
    "cidade",
    "uf",
    "bairro",
    "logradouro",
    "numero",
    "cep",
    "telefone",
    "email",
    "latitude",
    "longitude",
]

COLUNAS_EMPRESAS = [
    "cnpj_basico",
    "razao_social",
    "natureza_juridica",
    "qualificacao_responsavel",
    "capital_social",
    "porte",
    "ente_federativo",
]

COLUNAS_ESTABELECIMENTOS = [
    "cnpj_basico",
    "cnpj_ordem",
    "cnpj_dv",
    "matriz_filial",
    "nome_fantasia",
    "situacao_cadastral",
    "data_situacao_cadastral",
    "motivo_situacao_cadastral",
    "nome_cidade_exterior",
    "pais",
    "data_inicio_atividade",
    "cnae_principal",
    "cnae_secundaria",
    "tipo_logradouro",
    "logradouro",
    "numero",
    "complemento",
    "bairro",
    "cep",
    "uf",
    "municipio",
    "ddd1",
    "telefone1",
    "ddd2",
    "telefone2",
    "ddd_fax",
    "fax",
    "email",
    "situacao_especial",
    "data_situacao_especial",
]

SUPABASE_DDL = """
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS empresas (
    id BIGSERIAL PRIMARY KEY,
    cnpj TEXT,
    razao_social TEXT,
    nome_fantasia TEXT,
    cnae_principal TEXT,
    descricao_cnae TEXT,
    cidade TEXT,
    uf CHAR(2),
    bairro TEXT,
    logradouro TEXT,
    numero TEXT,
    cep TEXT,
    telefone TEXT,
    email TEXT,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    geom GEOGRAPHY(POINT, 4326)
);

DELETE FROM empresas a
USING empresas b
WHERE a.id < b.id
  AND a.cnpj IS NOT NULL
  AND a.cnpj = b.cnpj;

CREATE UNIQUE INDEX IF NOT EXISTS idx_empresas_cnpj_unique
ON empresas (cnpj)
WHERE cnpj IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_empresas_cidade
ON empresas (cidade);

CREATE INDEX IF NOT EXISTS idx_empresas_uf
ON empresas (uf);

CREATE INDEX IF NOT EXISTS idx_empresas_cnae
ON empresas (cnae_principal);

CREATE INDEX IF NOT EXISTS idx_empresas_geom
ON empresas USING GIST (geom);

CREATE OR REPLACE FUNCTION buscar_empresas_raio(
    lat_input DOUBLE PRECISION,
    lon_input DOUBLE PRECISION,
    raio_km DOUBLE PRECISION,
    limite_resultados INTEGER DEFAULT 1000,
    cnae_input TEXT DEFAULT NULL
)
RETURNS TABLE (
    cnpj TEXT,
    razao_social TEXT,
    nome_fantasia TEXT,
    cnae_principal TEXT,
    descricao_cnae TEXT,
    cidade TEXT,
    uf CHAR(2),
    bairro TEXT,
    logradouro TEXT,
    numero TEXT,
    cep TEXT,
    telefone TEXT,
    email TEXT,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    distancia_km DOUBLE PRECISION
)
LANGUAGE sql
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
            (
                ST_Distance(
                    e.geom,
                    ST_SetSRID(ST_MakePoint(lon_input, lat_input), 4326)::geography
                ) / 1000
            )::numeric,
            2
        )::double precision AS distancia_km
    FROM empresas e
    WHERE e.geom IS NOT NULL
      AND (
          cnae_input IS NULL
          OR cnae_input = ''
          OR e.cnae_principal ILIKE cnae_input || '%'
          OR e.descricao_cnae ILIKE '%' || cnae_input || '%'
      )
      AND ST_DWithin(
          e.geom,
          ST_SetSRID(ST_MakePoint(lon_input, lat_input), 4326)::geography,
          raio_km * 1000
      )
    ORDER BY distancia_km
    LIMIT limite_resultados;
$$;
"""


def normalizar_texto(valor):
    return (valor or "").strip().upper()


def somente_digitos(valor):
    return re.sub(r"\D", "", valor or "")


def montar_cnpj(cnpj_basico, ordem, dv):
    return f"{somente_digitos(cnpj_basico):0>8}{somente_digitos(ordem):0>4}{somente_digitos(dv):0>2}"


def montar_telefone(ddd, telefone):
    ddd = somente_digitos(ddd)
    telefone = somente_digitos(telefone)
    if not ddd or not telefone:
        return ""
    return f"({ddd}) {telefone}"


def carregar_env(caminho=BASE_DIR / ".env"):
    if not caminho.exists():
        return

    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, valor = linha.split("=", 1)
        os.environ.setdefault(chave.strip(), valor.strip().strip('"').strip("'"))


def valor_float(valor):
    if valor in (None, ""):
        return None
    try:
        return float(str(valor).replace(",", "."))
    except ValueError:
        return None


def request_get(url, **kwargs):
    response = requests.get(
        url,
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
        auth=(RF_SHARE_TOKEN, ""),
        **kwargs,
    )
    response.raise_for_status()
    return response


def baixar_html(url):
    return request_get(url).text


def webdav_listar(url, depth="1"):
    try:
        response = requests.request(
            "PROPFIND",
            url,
            headers={
                "Depth": depth,
                "Content-Type": "application/xml",
                **HEADERS,
            },
            data=WEBDAV_PROPFIND,
            auth=(RF_SHARE_TOKEN, ""),
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException as erro:
        raise RuntimeError(
            "Nao foi possivel consultar o WebDAV da Receita.\n"
            f"URL: {url}\n"
            f"Erro: {erro}"
        ) from erro

    try:
        tree = ET.fromstring(response.content)
    except ET.ParseError as erro:
        trecho = response.text[:500].replace("\n", " ")
        raise RuntimeError(
            "A Receita respondeu um conteudo que nao consegui ler como XML WebDAV.\n"
            f"Erro: {erro}\n"
            f"Inicio da resposta: {trecho}"
        ) from erro

    hrefs = []
    for element in tree.iter():
        if element.tag.endswith("href") and element.text:
            hrefs.append(element.text)

    return hrefs


def descobrir_pasta_recente():
    hrefs = webdav_listar(WEBDAV_URL)
    pastas = []

    for href in hrefs:
        match = re.search(r"/(\d{4}-\d{2})/?$", href)
        if match:
            pastas.append(match.group(1))

    if not pastas:
        raise RuntimeError(f"Nenhuma pasta AAAA-MM foi encontrada no WebDAV: {hrefs}")

    return sorted(pastas)[-1]


def listar_zips_da_pasta(pasta):
    hrefs = webdav_listar(f"{WEBDAV_URL}{pasta}/")
    arquivos = []

    for href in hrefs:
        nome = unquote(href.split("/")[-1])
        if nome.lower().endswith(".zip"):
            arquivos.append(
                {
                    "nome": nome,
                    "url": f"{WEBDAV_URL}{pasta}/{quote(nome)}",
                }
            )

    return sorted(arquivos, key=lambda item: item["nome"])


def filtrar_zips(arquivos):
    tipos = ("Empresas", "Estabelecimentos", "Cnaes", "Municipios")
    return [
        arquivo
        for arquivo in arquivos
        if any(tipo.lower() in nome_arquivo(arquivo).lower() for tipo in tipos)
    ]


def arquivos_padrao():
    arquivos = ["Cnaes.zip", "Municipios.zip"]
    arquivos.extend(f"Empresas{i}.zip" for i in range(10))
    arquivos.extend(f"Estabelecimentos{i}.zip" for i in range(10))
    return arquivos


def nome_arquivo(arquivo):
    if isinstance(arquivo, dict):
        return arquivo["nome"]
    return arquivo


def url_arquivo(arquivo):
    if isinstance(arquivo, dict):
        return arquivo["url"]
    return arquivo


def salvar_links_download(arquivos):
    with ARQUIVO_LINKS.open("w", encoding="utf-8") as f:
        for arquivo in arquivos:
            f.write(url_arquivo(arquivo) + "\n")
    return ARQUIVO_LINKS


def baixar_arquivo(arquivo):
    PASTA_DOWNLOAD.mkdir(parents=True, exist_ok=True)
    nome = nome_arquivo(arquivo)
    url = url_arquivo(arquivo)
    destino = PASTA_DOWNLOAD / nome

    if destino.exists() and destino.stat().st_size > 0 and zipfile.is_zipfile(destino):
        print(f"Ja existe: {destino.name}")
        return destino
    if destino.exists():
        print(f"Arquivo invalido ou vazio, baixando novamente: {destino.name}")
        destino.unlink()

    temporario = destino.with_suffix(destino.suffix + ".part")
    temporario.unlink(missing_ok=True)

    print(f"Baixando: {nome}")
    try:
        response = request_get(url, stream=True)
    except requests.RequestException as erro:
        raise RuntimeError(
            "Nao foi possivel baixar arquivos da Receita a partir desta rede.\n"
            f"Falhou em: {url}\n"
            f"Erro: {erro}\n\n"
            "Baixe os ZIPs manualmente para a pasta dados_receita e rode:\n"
            "python .\\cnpjs_receita.py --sem-download"
        ) from erro

    with response:
        total = int(response.headers.get("content-length", 0))
        baixado = 0

        with temporario.open("wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                baixado += len(chunk)
                if total:
                    percentual = baixado / total * 100
                    print(
                        f"\r  {percentual:6.2f}% ({baixado / 1024 / 1024:.1f} MB)",
                        end="",
                    )

    if baixado == 0:
        temporario.unlink(missing_ok=True)
        raise RuntimeError(
            f"Download vazio para {nome}. A URL retornou 0 bytes: {url}"
        )

    if not zipfile.is_zipfile(temporario):
        temporario.unlink(missing_ok=True)
        raise RuntimeError(
            f"O arquivo baixado nao e um ZIP valido: {nome}\nURL: {url}"
        )

    temporario.replace(destino)
    print(f"\nOK: {destino.name}")
    return destino


def linhas_zip(caminho_zip):
    with zipfile.ZipFile(caminho_zip) as zf:
        nomes = zf.namelist()
        if not nomes:
            return
        with zf.open(nomes[0]) as arquivo:
            texto = (linha.decode("latin1").rstrip("\r\n") for linha in arquivo)
            yield from csv.reader(texto, delimiter=";", quotechar='"')


def carregar_municipios(caminhos):
    municipios = {}
    codigos_alvo = set()

    for caminho in caminhos:
        for linha in linhas_zip(caminho):
            if len(linha) < 2:
                continue
            codigo, cidade = linha[0].strip(), normalizar_texto(linha[1])
            municipios[codigo] = cidade
            if cidade in CIDADES:
                codigos_alvo.add(codigo)

    if not codigos_alvo:
        raise RuntimeError("Nenhum codigo das cidades alvo foi encontrado em Municipios.zip.")

    return municipios, codigos_alvo


def carregar_cnaes(caminhos):
    cnaes = {}

    for caminho in caminhos:
        for linha in linhas_zip(caminho):
            if len(linha) >= 2:
                cnaes[linha[0].strip()] = linha[1].strip()

    return cnaes


def coletar_estabelecimentos(caminhos, municipios, codigos_alvo):
    estabelecimentos = {}
    cnpjs_basicos = set()

    for caminho in caminhos:
        print(f"Lendo estabelecimentos: {caminho.name}")
        for linha in linhas_zip(caminho):
            if len(linha) < len(COLUNAS_ESTABELECIMENTOS):
                continue

            row = dict(zip(COLUNAS_ESTABELECIMENTOS, linha))
            if row["uf"].strip().upper() != "PR":
                continue
            if row["municipio"].strip() not in codigos_alvo:
                continue
            if row["situacao_cadastral"].strip() != "02":
                continue

            cnpj = montar_cnpj(row["cnpj_basico"], row["cnpj_ordem"], row["cnpj_dv"])
            if cnpj in estabelecimentos:
                continue

            cidade = municipios.get(row["municipio"].strip(), "")
            telefone = montar_telefone(row["ddd1"], row["telefone1"])
            logradouro = " ".join(
                parte.strip()
                for parte in [row["tipo_logradouro"], row["logradouro"]]
                if parte.strip()
            )

            estabelecimentos[cnpj] = {
                "cnpj": cnpj,
                "cnpj_basico": row["cnpj_basico"].strip(),
                "razao_social": "",
                "nome_fantasia": row["nome_fantasia"].strip(),
                "cnae_principal": row["cnae_principal"].strip(),
                "descricao_cnae": "",
                "cidade": cidade,
                "uf": row["uf"].strip().upper(),
                "bairro": row["bairro"].strip(),
                "logradouro": logradouro,
                "numero": row["numero"].strip(),
                "cep": somente_digitos(row["cep"]),
                "telefone": telefone,
                "email": row["email"].strip().lower(),
                "latitude": "",
                "longitude": "",
            }
            cnpjs_basicos.add(row["cnpj_basico"].strip())

    return estabelecimentos, cnpjs_basicos


def preencher_empresas(caminhos, estabelecimentos, cnpjs_basicos):
    empresas = {}

    for caminho in caminhos:
        print(f"Lendo empresas: {caminho.name}")
        for linha in linhas_zip(caminho):
            if len(linha) < len(COLUNAS_EMPRESAS):
                continue
            row = dict(zip(COLUNAS_EMPRESAS, linha))
            cnpj_basico = row["cnpj_basico"].strip()
            if cnpj_basico in cnpjs_basicos:
                empresas[cnpj_basico] = row["razao_social"].strip()

    for item in estabelecimentos.values():
        if item["cnpj_basico"] in empresas:
            item["razao_social"] = empresas[item["cnpj_basico"]]


def preencher_cnaes(estabelecimentos, cnaes):
    for item in estabelecimentos.values():
        item["descricao_cnae"] = cnaes.get(item["cnae_principal"], "")


def carregar_geocode_cache():
    if not ARQUIVO_GEOCODE_CACHE.exists():
        return {}
    try:
        return json.loads(ARQUIVO_GEOCODE_CACHE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def salvar_geocode_cache(cache):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ARQUIVO_GEOCODE_CACHE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def endereco_para_geocode(item):
    partes = [
        item.get("logradouro"),
        item.get("numero"),
        item.get("bairro"),
        item.get("cidade"),
        item.get("uf"),
        item.get("cep"),
        "Brasil",
    ]
    return ", ".join(str(parte).strip() for parte in partes if parte)


def geocodificar_endereco(endereco):
    params = {
        "q": endereco,
        "format": "json",
        "limit": "1",
        "countrycodes": "br",
        "addressdetails": "0",
    }
    headers = {
        "User-Agent": (
            "prospector-cnpj-etl/1.0 "
            f"({os.getenv('NOMINATIM_EMAIL', 'contato-local')})"
        )
    }
    response = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params=params,
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if not data:
        return None
    return {
        "latitude": float(data[0]["lat"]),
        "longitude": float(data[0]["lon"]),
    }


def geocodificar_estabelecimentos(estabelecimentos, limite=1000, delay=1.1):
    cache = carregar_geocode_cache()
    processados = 0
    encontrados = 0

    for item in estabelecimentos.values():
        if item.get("latitude") and item.get("longitude"):
            continue
        if limite and processados >= limite:
            break

        endereco = endereco_para_geocode(item)
        if not endereco:
            continue

        chave = normalizar_texto(endereco)
        if chave in cache:
            resultado = cache[chave]
        else:
            try:
                resultado = geocodificar_endereco(endereco)
            except requests.RequestException as erro:
                print(f"Geocode falhou: {endereco} -> {erro}")
                resultado = None
            cache[chave] = resultado
            salvar_geocode_cache(cache)
            time.sleep(delay)

        processados += 1
        if resultado:
            item["latitude"] = resultado["latitude"]
            item["longitude"] = resultado["longitude"]
            encontrados += 1

        if processados % 25 == 0:
            print(f"Geocodificados: {processados} tentativas, {encontrados} encontrados")

    salvar_geocode_cache(cache)
    print(f"Geocodificacao concluida: {processados} tentativas, {encontrados} encontrados")


def geocodificar_supabase_pendentes(limite=1000, delay=1.1):
    conn = conectar_supabase()
    cache = carregar_geocode_cache()
    processados = 0
    encontrados = 0

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(SUPABASE_DDL)
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
                    ORDER BY cidade, id
                    LIMIT %s
                    """,
                    (list(CIDADES), limite if limite else 2147483647),
                )
                rows = cur.fetchall()

                print(f"Registros pendentes para geocodificar: {len(rows)}")

                for row in rows:
                    item = {
                        "id": row[0],
                        "cnpj": row[1],
                        "cidade": row[2],
                        "uf": row[3],
                        "bairro": row[4],
                        "logradouro": row[5],
                        "numero": row[6],
                        "cep": row[7],
                    }
                    endereco = endereco_para_geocode(item)
                    if not endereco:
                        continue

                    chave = normalizar_texto(endereco)
                    if chave in cache:
                        resultado = cache[chave]
                    else:
                        try:
                            resultado = geocodificar_endereco(endereco)
                        except requests.RequestException as erro:
                            print(f"Geocode falhou: {endereco} -> {erro}")
                            resultado = None
                        cache[chave] = resultado
                        salvar_geocode_cache(cache)
                        time.sleep(delay)

                    processados += 1
                    if resultado:
                        cur.execute(
                            """
                            UPDATE empresas
                            SET
                                latitude = %s,
                                longitude = %s,
                                geom = ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
                            WHERE id = %s
                            """,
                            (
                                resultado["latitude"],
                                resultado["longitude"],
                                resultado["longitude"],
                                resultado["latitude"],
                                item["id"],
                            ),
                        )
                        encontrados += 1

                    if processados % 25 == 0:
                        conn.commit()
                        print(
                            f"Geocodificados no Supabase: "
                            f"{processados} tentativas, {encontrados} encontrados"
                        )

                conn.commit()
    finally:
        conn.close()

    salvar_geocode_cache(cache)
    print(
        f"Geocodificacao no Supabase concluida: "
        f"{processados} tentativas, {encontrados} encontrados"
    )


def salvar_csv(estabelecimentos, arquivo_saida):
    arquivo_saida.parent.mkdir(parents=True, exist_ok=True)

    if arquivo_saida.suffix.lower() == ".gz":
        opener = gzip.open
    else:
        opener = open

    with opener(arquivo_saida, "wt", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUNAS_SAIDA)
        writer.writeheader()

        for item in sorted(estabelecimentos.values(), key=lambda row: row["cnpj"]):
            writer.writerow({coluna: item.get(coluna, "") for coluna in COLUNAS_SAIDA})


def apagar_zips(caminhos):
    apagados = 0
    for caminho in caminhos:
        try:
            Path(caminho).unlink()
            apagados += 1
        except FileNotFoundError:
            continue

    return apagados


def apagar_se_permitido(caminhos, manter_zips):
    if manter_zips:
        return 0
    return apagar_zips(caminhos)


def conectar_supabase():
    if psycopg2 is None:
        raise RuntimeError(
            "Dependencia ausente: instale psycopg2-binary com:\n"
            "pip install psycopg2-binary"
        )

    carregar_env()

    database_url = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DATABASE_URL")
    if database_url:
        return psycopg2.connect(database_url, sslmode="require")

    host = os.getenv("SUPABASE_HOST") or os.getenv("PG_HOST")
    port = os.getenv("SUPABASE_PORT") or os.getenv("PG_PORT") or "5432"
    dbname = os.getenv("SUPABASE_DB") or os.getenv("PG_DB") or "postgres"
    user = os.getenv("SUPABASE_USER") or os.getenv("PG_USER") or "postgres"
    password = os.getenv("SUPABASE_PASSWORD") or os.getenv("PG_PASS")
    project_ref = os.getenv("SUPABASE_PROJECT_REF")

    if not host or not password:
        raise RuntimeError(
            "Credenciais do Supabase nao encontradas no .env.\n"
            "Use SUPABASE_HOST, SUPABASE_PORT, SUPABASE_DB, "
            "SUPABASE_USER e SUPABASE_PASSWORD."
        )

    if "pooler.supabase.com" in host and "." not in user:
        if project_ref:
            user = f"{user}.{project_ref}"
        else:
            raise RuntimeError(
                "O Supabase pooler exige o identificador do projeto no usuario.\n"
                "No seu .env, adicione uma destas opcoes:\n\n"
                "1) SUPABASE_PROJECT_REF=seu_project_ref\n"
                "   mantendo SUPABASE_USER=postgres\n\n"
                "ou\n\n"
                "2) SUPABASE_USER=postgres.seu_project_ref\n\n"
                "O project_ref aparece na URL do projeto no Supabase, como:\n"
                "https://supabase.com/dashboard/project/SEU_PROJECT_REF"
            )

    try:
        return psycopg2.connect(
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            password=password,
            sslmode="require",
        )
    except psycopg2.OperationalError as erro:
        mensagem = str(erro)
        if "ENOIDENTIFIER" in mensagem or "no tenant identifier" in mensagem:
            raise RuntimeError(
                "Falha ao conectar no Supabase pooler: falta o identificador do projeto.\n"
                "Ajuste o .env com SUPABASE_PROJECT_REF=seu_project_ref "
                "ou SUPABASE_USER=postgres.seu_project_ref."
            ) from erro
        raise


def validar_conexao_supabase():
    conn = conectar_supabase()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        print("Conexao com Supabase OK.")
    finally:
        conn.close()


def preparar_linha_supabase(item):
    latitude = valor_float(item.get("latitude"))
    longitude = valor_float(item.get("longitude"))

    return (
        item.get("cnpj") or None,
        item.get("razao_social") or None,
        item.get("nome_fantasia") or None,
        item.get("cnae_principal") or None,
        item.get("descricao_cnae") or None,
        item.get("cidade") or None,
        item.get("uf") or None,
        item.get("bairro") or None,
        item.get("logradouro") or None,
        item.get("numero") or None,
        item.get("cep") or None,
        item.get("telefone") or None,
        item.get("email") or None,
        latitude,
        longitude,
    )


def enviar_supabase(estabelecimentos, batch_size=5000):
    sql = """
        INSERT INTO empresas (
            cnpj,
            razao_social,
            nome_fantasia,
            cnae_principal,
            descricao_cnae,
            cidade,
            uf,
            bairro,
            logradouro,
            numero,
            cep,
            telefone,
            email,
            latitude,
            longitude,
            geom
        )
        VALUES %s
        ON CONFLICT (cnpj) WHERE cnpj IS NOT NULL DO UPDATE SET
            razao_social = EXCLUDED.razao_social,
            nome_fantasia = EXCLUDED.nome_fantasia,
            cnae_principal = EXCLUDED.cnae_principal,
            descricao_cnae = EXCLUDED.descricao_cnae,
            cidade = EXCLUDED.cidade,
            uf = EXCLUDED.uf,
            bairro = EXCLUDED.bairro,
            logradouro = EXCLUDED.logradouro,
            numero = EXCLUDED.numero,
            cep = EXCLUDED.cep,
            telefone = EXCLUDED.telefone,
            email = EXCLUDED.email,
            latitude = EXCLUDED.latitude,
            longitude = EXCLUDED.longitude,
            geom = EXCLUDED.geom
    """
    template = """
        (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            CASE
                WHEN %s IS NOT NULL AND %s IS NOT NULL
                THEN ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
                ELSE NULL
            END
        )
    """

    linhas = []
    for item in estabelecimentos.values():
        row = preparar_linha_supabase(item)
        latitude = row[13]
        longitude = row[14]
        linhas.append((*row, latitude, longitude, longitude, latitude))

    if not linhas:
        print("Nenhum registro para enviar ao Supabase.")
        return 0

    conn = conectar_supabase()
    try:
        with conn:
            with conn.cursor() as cur:
                print("Verificando estrutura da tabela empresas no Supabase...")
                cur.execute(SUPABASE_DDL)

                total = 0
                for inicio in range(0, len(linhas), batch_size):
                    batch = linhas[inicio:inicio + batch_size]
                    psycopg2.extras.execute_values(
                        cur,
                        sql,
                        batch,
                        template=template,
                        page_size=1000,
                    )
                    total += len(batch)
                    print(f"Enviados ao Supabase: {total}/{len(linhas)}")

        return len(linhas)
    finally:
        conn.close()


def separar_por_tipo(caminhos):
    por_tipo = {
        "empresas": [],
        "estabelecimentos": [],
        "cnaes": [],
        "municipios": [],
    }

    for caminho in caminhos:
        nome = nome_arquivo(caminho)
        if hasattr(nome, "name"):
            nome = nome.name
        nome = str(nome).lower()
        if "empresa" in nome:
            por_tipo["empresas"].append(caminho)
        elif "estabelecimento" in nome:
            por_tipo["estabelecimentos"].append(caminho)
        elif "cnae" in nome:
            por_tipo["cnaes"].append(caminho)
        elif "municipio" in nome:
            por_tipo["municipios"].append(caminho)

    return por_tipo


def validar_tipos_obrigatorios(por_tipo):
    obrigatorios = ["empresas", "estabelecimentos", "cnaes", "municipios"]
    faltando = [tipo for tipo in obrigatorios if not por_tipo[tipo]]
    if faltando:
        raise RuntimeError(f"Arquivos obrigatorios ausentes: {', '.join(faltando)}")


def processar_com_downloads(arquivos, manter_zips):
    por_tipo = separar_por_tipo(arquivos)
    validar_tipos_obrigatorios(por_tipo)

    caminhos_cnaes = [baixar_arquivo(arquivo) for arquivo in por_tipo["cnaes"]]
    print("Carregando CNAEs...")
    cnaes = carregar_cnaes(caminhos_cnaes)
    apagar_se_permitido(caminhos_cnaes, manter_zips)

    caminhos_municipios = [baixar_arquivo(arquivo) for arquivo in por_tipo["municipios"]]
    print("Carregando municipios...")
    municipios, codigos_alvo = carregar_municipios(caminhos_municipios)
    apagar_se_permitido(caminhos_municipios, manter_zips)

    estabelecimentos = {}
    cnpjs_basicos = set()
    for arquivo in por_tipo["estabelecimentos"]:
        caminho = baixar_arquivo(arquivo)
        encontrados, basicos = coletar_estabelecimentos([caminho], municipios, codigos_alvo)
        estabelecimentos.update(encontrados)
        cnpjs_basicos.update(basicos)
        print(f"Estabelecimentos filtrados ate agora: {len(estabelecimentos)}")
        apagar_se_permitido([caminho], manter_zips)

    for arquivo in por_tipo["empresas"]:
        caminho = baixar_arquivo(arquivo)
        preencher_empresas([caminho], estabelecimentos, cnpjs_basicos)
        apagar_se_permitido([caminho], manter_zips)

    preencher_cnaes(estabelecimentos, cnaes)
    return estabelecimentos


def processar_zips_locais(caminhos, manter_zips):
    por_tipo = separar_por_tipo(caminhos)
    validar_tipos_obrigatorios(por_tipo)

    print("Carregando municipios...")
    municipios, codigos_alvo = carregar_municipios(por_tipo["municipios"])

    print("Carregando CNAEs...")
    cnaes = carregar_cnaes(por_tipo["cnaes"])

    estabelecimentos, cnpjs_basicos = coletar_estabelecimentos(
        por_tipo["estabelecimentos"], municipios, codigos_alvo
    )
    print(f"Estabelecimentos filtrados: {len(estabelecimentos)}")

    preencher_empresas(por_tipo["empresas"], estabelecimentos, cnpjs_basicos)
    preencher_cnaes(estabelecimentos, cnaes)

    apagar_se_permitido(caminhos, manter_zips)
    return estabelecimentos


def main():
    parser = argparse.ArgumentParser(
        description="Baixa a base CNPJ da Receita, filtra Maringa/regiao e envia ao Supabase."
    )
    parser.add_argument(
        "--sem-download",
        action="store_true",
        help="Usa somente os ZIPs ja existentes em dados_receita.",
    )
    parser.add_argument(
        "--saida",
        default=str(ARQUIVO_SAIDA),
        help="Arquivo CSV opcional. Padrao: database/sample_data.csv.gz",
    )
    parser.add_argument(
        "--gerar-csv",
        action="store_true",
        help="Tambem gera CSV local com a base reduzida.",
    )
    parser.add_argument(
        "--manter-zips",
        action="store_true",
        help="Nao apaga os ZIPs baixados depois do envio ao Supabase.",
    )
    parser.add_argument(
        "--versao",
        help="Versao manual no formato AAAA-MM. Se omitida, usa a mais recente do WebDAV.",
    )
    parser.add_argument(
        "--geocodificar",
        action="store_true",
        help="Geocodifica enderecos com Nominatim antes de enviar ao Supabase.",
    )
    parser.add_argument(
        "--geocodificar-supabase",
        action="store_true",
        help="Geocodifica registros ja existentes no Supabase e atualiza lat/lon/geom.",
    )
    parser.add_argument(
        "--limite-geocode",
        type=int,
        default=1000,
        help="Maximo de enderecos geocodificados por execucao. Use 0 para sem limite.",
    )
    parser.add_argument(
        "--geocode-delay",
        type=float,
        default=1.1,
        help="Intervalo em segundos entre chamadas ao Nominatim.",
    )
    parser.add_argument(
        "--apenas-schema",
        action="store_true",
        help="Cria/atualiza tabela, indices e funcoes no Supabase sem baixar dados.",
    )
    args = parser.parse_args()

    validar_conexao_supabase()

    if args.apenas_schema:
        conn = conectar_supabase()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(SUPABASE_DDL)
            print("Schema/funcoes atualizados no Supabase.")
        finally:
            conn.close()
        return

    if args.geocodificar_supabase:
        geocodificar_supabase_pendentes(
            limite=args.limite_geocode,
            delay=args.geocode_delay,
        )
        return

    if args.sem_download:
        caminhos = sorted(PASTA_DOWNLOAD.glob("*.zip"))
        if not caminhos:
            raise RuntimeError("Nenhum ZIP encontrado em dados_receita.")
        estabelecimentos = processar_zips_locais(caminhos, args.manter_zips)
    else:
        if args.versao:
            versao = args.versao
        else:
            print("Buscando versao mais recente da base CNPJ via WebDAV...")
            versao = descobrir_pasta_recente()

        print(f"Versao encontrada: {versao}")
        print(f"WebDAV: {WEBDAV_URL}{versao}/")

        arquivos = filtrar_zips(listar_zips_da_pasta(versao))

        print(f"Arquivos selecionados: {len(arquivos)}")
        links = salvar_links_download(arquivos)
        print(f"Links diretos salvos em: {links}")

        estabelecimentos = processar_com_downloads(arquivos, args.manter_zips)

    if args.geocodificar:
        geocodificar_estabelecimentos(
            estabelecimentos,
            limite=args.limite_geocode,
            delay=args.geocode_delay,
        )

    enviados = enviar_supabase(estabelecimentos)
    print(f"Registros enviados ao Supabase: {enviados}")

    if args.gerar_csv:
        arquivo_saida = Path(args.saida)
        salvar_csv(estabelecimentos, arquivo_saida)
        print(f"CSV gerado: {arquivo_saida}")

    print("ETL finalizado com sucesso.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as erro:
        print()
        print("ERRO:")
        print(erro)
        sys.exit(1)
