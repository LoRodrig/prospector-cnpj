# 🔍 Prospector CNPJ

[![ETL Mensal](https://github.com/SEU_USUARIO/prospector-cnpj/actions/workflows/etl_mensal.yml/badge.svg)](https://github.com/SEU_USUARIO/prospector-cnpj/actions/workflows/etl_mensal.yml)
[![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Supabase](https://img.shields.io/badge/Supabase-PostGIS-3ECF8E?logo=supabase&logoColor=white)](https://supabase.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Leaflet](https://img.shields.io/badge/Leaflet-1.9-199900?logo=leaflet&logoColor=white)](https://leafletjs.com/)
[![GitHub Pages](https://img.shields.io/badge/GitHub%20Pages-live-222222?logo=github&logoColor=white)](https://SEU_USUARIO.github.io/prospector-cnpj/)
[![Dados Abertos](https://img.shields.io/badge/Dados-Receita%20Federal-009C3B)](https://dados.gov.br/dados/conjuntos-dados/cadastro-nacional-da-pessoa-juridica---cnpj)
[![Licença](https://img.shields.io/badge/licen%C3%A7a-MIT-blue)](LICENSE)

**Pipeline de Engenharia de Dados** que coleta empresas dos dados abertos da Receita Federal, armazena em PostgreSQL com extensão PostGIS no Supabase e disponibiliza consulta geoespacial por raio de distância via interface web.

> 🌐 **[Ver demo ao vivo →](https://SEU_USUARIO.github.io/prospector-cnpj/)**

![Demo do Prospector CNPJ](docs/demo.gif)

---

## ✨ Funcionalidades

| Feature | Detalhe |
|---|---|
| 🗺 **Mapa interativo** | Pins coloridos por setor CNAE via Leaflet.js |
| 📍 **Busca geoespacial** | Raio de distância com PostGIS `ST_DWithin` |
| 📊 **Gráficos de análise** | Top 10 CNAEs, distribuição por cidade, presença de contato |
| 🔍 **Autocomplete CNAE** | Filtro por setor ou código com lista dinâmica |
| 📋 **Painel de detalhes** | Drawer lateral com links para Google Maps e OSM |
| 💾 **Exportação CSV** | Download com BOM UTF-8 para Excel brasileiro |
| 🤖 **ETL automatizado** | GitHub Actions roda todo mês, coleta novos dados da Receita |
| 🔄 **Geocodificação** | Endereço → lat/lon via Nominatim com cache local e fallback por cidade |

---

## 🏗 Arquitetura

```
┌─────────────────────────────────────────────────────────────────────┐
│                         PIPELINE DE DADOS                           │
│                                                                     │
│  Receita Federal          ETL Python           Supabase             │
│  (dados.gov.br)     ─►   cnpjs_receita.py  ─►  PostgreSQL          │
│   ZIPs mensais            ├ download           + PostGIS            │
│   ~7 GB total             ├ parse CSV          ├ tabela empresas    │
│   ~60M empresas           ├ filtro por cidade  ├ índice GIST        │
│                           └ carga em batch     └ função RPC         │
│                                                                     │
│                      geocodificar_supabase.py                       │
│                           └ Nominatim API ──► lat/lon/geom          │
│                                                                     │
│                      GitHub Actions (cron mensal)                   │
│                           └ executa ETL + geocodificação            │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                         FRONTEND (GitHub Pages)                     │
│                                                                     │
│  index.html + app.js + styles.css                                   │
│       │                                                             │
│       ├─ Nominatim API  ──► geocodifica cidade digitada             │
│       ├─ Supabase RPC   ──► buscar_empresas_raio(lat, lon, km)      │
│       ├─ Leaflet.js     ──► renderiza pins no mapa                  │
│       └─ Chart.js       ──► gráficos de CNAE e distribuição         │
└─────────────────────────────────────────────────────────────────────┘
```

> Para um diagrama visual, veja [`docs/arquitetura.png`](docs/arquitetura.png).

---

## 📁 Estrutura do Repositório

```
prospector-cnpj/
├── .github/
│   └── workflows/
│       └── etl_mensal.yml       # GitHub Actions — execução mensal automática
│
├── etl/
│   ├── cnpjs_receita.py         # Pipeline principal: download → parse → Supabase
│   ├── geocodificar_supabase.py # Geocodificação de endereços via Nominatim
│   └── requirements.txt         # Dependências Python (requests, psycopg2-binary)
│
├── sql/
│   ├── schema.sql               # DDL: tabela empresas + extensão PostGIS + índice
│   └── functions.sql            # Função buscar_empresas_raio (PostGIS RPC)
│
├── frontend/
│   ├── index.html               # Estrutura da página
│   ├── app.js                   # Lógica: busca, mapa, gráficos, CSV
│   ├── styles.css               # Estilos (dark theme, grid, componentes)
│   ├── config.js                # ⚠ NÃO versionar — criado a partir de config.example.js
│   └── config.example.js        # Template de configuração (commitar este)
│
├── docs/
│   ├── demo.gif                 # GIF animado para o README
│   └── arquitetura.png          # Diagrama de arquitetura
│
├── database/
│   └── sample_data.csv          # ~200 empresas de exemplo para demo
│
├── .gitignore
├── LICENSE
└── README.md
```

---

## 🚀 Como rodar localmente

### Pré-requisitos
- Python 3.11+
- Conta no [Supabase](https://supabase.com/) (plano gratuito funciona)
- PostgreSQL com extensão PostGIS habilitada no Supabase

### Passos

```bash
# 1. Clone o repositório
git clone https://github.com/SEU_USUARIO/prospector-cnpj.git
cd prospector-cnpj

# 2. Instale as dependências Python
pip install -r etl/requirements.txt

# 3. Configure as credenciais
cp frontend/config.example.js frontend/config.js
# Edite frontend/config.js com sua SUPABASE_URL e SUPABASE_ANON_KEY

cp etl/.env.example etl/.env
# Edite etl/.env com suas credenciais do banco

# 4. Execute o ETL (baixa ~7GB da Receita, processa e sobe para o Supabase)
cd etl
python cnpjs_receita.py

# 5. Geocodifique os endereços
python geocodificar_supabase.py --limite 1000

# 6. Abra o frontend
# Basta abrir frontend/index.html no navegador, ou usar Live Server no VS Code
```

---

## ⚙️ Configuração do Supabase

### 1. Habilitar PostGIS
No SQL Editor do Supabase:
```sql
CREATE EXTENSION IF NOT EXISTS postgis;
```

### 2. Criar a tabela
Execute o conteúdo de [`sql/schema.sql`](sql/schema.sql).

### 3. Criar a função RPC
Execute o conteúdo de [`sql/functions.sql`](sql/functions.sql).

### 4. Variáveis de ambiente necessárias (arquivo `etl/.env`)
```env
SUPABASE_HOST=aws-1-us-west-1.pooler.supabase.com
SUPABASE_PORT=6543
SUPABASE_DB=postgres
SUPABASE_USER=postgres
SUPABASE_PROJECT_REF=seu_project_ref
SUPABASE_PASSWORD=sua_senha
NOMINATIM_EMAIL=seu@email.com
```

---

## 🤖 GitHub Actions — ETL Automático

O workflow [`.github/workflows/etl_mensal.yml`](.github/workflows/etl_mensal.yml) executa automaticamente no **dia 15 de cada mês** e pode ser acionado manualmente a qualquer momento.

### Configurar os Secrets no GitHub

Acesse: **Settings → Secrets and variables → Actions → New repository secret**

| Secret | Valor |
|--------|-------|
| `SUPABASE_HOST` | Host do pooler do Supabase |
| `SUPABASE_PORT` | `6543` |
| `SUPABASE_DB` | `postgres` |
| `SUPABASE_USER` | `postgres` |
| `SUPABASE_PASSWORD` | Senha do banco |
| `SUPABASE_PROJECT_REF` | Project ref (aparece na URL do Supabase) |
| `NOMINATIM_EMAIL` | Seu e-mail (exigido pela política do Nominatim) |

### Executar manualmente

Na aba **Actions** → **ETL Mensal — Receita Federal** → **Run workflow**.

---

## 🗄️ Modelo de Dados

```sql
CREATE TABLE empresas (
  id              BIGSERIAL PRIMARY KEY,
  cnpj            TEXT UNIQUE NOT NULL,
  razao_social    TEXT,
  nome_fantasia   TEXT,
  cnae_principal  TEXT,
  descricao_cnae  TEXT,
  cidade          TEXT,
  uf              CHAR(2),
  bairro          TEXT,
  logradouro      TEXT,
  numero          TEXT,
  cep             TEXT,
  telefone        TEXT,
  email           TEXT,
  latitude        DOUBLE PRECISION,
  longitude       DOUBLE PRECISION,
  geom            GEOGRAPHY(POINT, 4326)  -- PostGIS, indexado com GIST
);

-- Índice espacial para consultas de raio
CREATE INDEX idx_empresas_geom ON empresas USING GIST (geom);
```

---

## 📊 Volume de Dados

| Escopo | Registros |
|--------|-----------|
| Brasil total (Receita Federal) | ~60 milhões de CNPJs |
| Região de Maringá (demo) | ~130.000 empresas ativas |
| Com e-mail | ~65% |
| Com telefone | ~90% |
| Com geolocalização (após ETL) | ~85% |

---

## 🛠 Tecnologias

| Camada | Tecnologia |
|--------|-----------|
| ETL | Python 3.11, requests, psycopg2 |
| Banco de dados | PostgreSQL 16 + PostGIS |
| BaaS | Supabase (hosting + API + autenticação) |
| Geocodificação | Nominatim / OpenStreetMap |
| Frontend | HTML5, CSS3, JavaScript (vanilla) |
| Mapa | Leaflet.js |
| Gráficos | Chart.js |
| CI/CD | GitHub Actions |
| Hospedagem | GitHub Pages |

---

## 📄 Licença

Este projeto está sob a licença MIT. Veja o arquivo [LICENSE](LICENSE) para mais detalhes.

Os dados são públicos e disponibilizados pela Receita Federal do Brasil sob [dados.gov.br](https://dados.gov.br/).

---

<p align="center">
  Feito com ☕ e dados abertos · 
  <a href="https://SEU_USUARIO.github.io/prospector-cnpj/">Ver demo</a> ·
  <a href="https://github.com/SEU_USUARIO/prospector-cnpj/issues">Reportar bug</a>
</p>
