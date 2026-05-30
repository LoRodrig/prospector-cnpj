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


def geocodificar(consulta, user_agent):
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


def buscar_pendentes(conn, limite, cidades, ignorar_ids):
    with conn.cursor() as cur:
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


def atualizar_coordenada(conn, empresa_id, lat, lon):
    with conn.cursor() as cur:
        cur.execute(
            """
            SET LOCAL statement_timeout = '60s';
            UPDATE empresas
            SET
                latitude = %s,
                longitude = %s,
                geom = ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
            WHERE id = %s
            """,
            (lat, lon, lon, lat, empresa_id),
        )


def tentar_atualizar_coordenada(conn, empresa_id, cnpj, lat, lon):
    try:
        atualizar_coordenada(conn, empresa_id, lat, lon)
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
    args = parser.parse_args()

    email = os.getenv("NOMINATIM_EMAIL", "rodri-local")
    user_agent = f"prospector-cnpj-geocoder/1.0 ({email})"
    cidades = [normalizar(c) for c in (args.cidade or CIDADES_PADRAO)]

    conn = conectar_supabase()
    cache = carregar_cache()
    atualizados = 0
    tentativas = 0
    processados = 0
    lote_atual = 0
    ids_tentados = set()
    inicio = time.monotonic()
    inicio_relogio = agora()

    print(f"Inicio: {inicio_relogio}")

    try:
        while True:
            if args.limite and processados >= args.limite:
                break
            if args.max_lotes and lote_atual >= args.max_lotes:
                break

            tamanho_lote = args.lote
            if args.limite:
                tamanho_lote = min(tamanho_lote, args.limite - processados)

            rows = buscar_pendentes(conn, tamanho_lote, cidades, ids_tentados)
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
                    chave = normalizar(json.dumps(consulta, sort_keys=True, ensure_ascii=False))
                    if chave in cache:
                        resultado = cache[chave]
                    else:
                        try:
                            resultado = geocodificar(consulta, user_agent)
                        except requests.RequestException as erro:
                            print(f"Falha Nominatim CNPJ {cnpj}: {erro}")
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
