
  /* â”€â”€â”€ CONFIG â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
(() => {
  'use strict';

  const { SUPABASE_URL, SUPABASE_ANON_KEY } = window.APP_CONFIG || {};
  const configOk = Boolean(
    SUPABASE_URL &&
    SUPABASE_ANON_KEY &&
    !String(SUPABASE_URL).includes('SEU_PROJECT_REF') &&
    !String(SUPABASE_ANON_KEY).includes('SUA_SUPABASE')
  );
  const dbClient = configOk ? window.supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY) : null;

  /* â”€â”€â”€ STATE â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
  let allData       = [];
  let filteredData  = [];
  let filtroAtivo   = 'todos';
  let sortCol       = 'distancia_km';
  let sortDir       = 'asc';
  let page          = 1;
  const PAGE_SIZE   = 50;
  let map;
  let markerLayer;
  let selectedLayer;
  let cnaeChart;
  let lastGeo = null;
  let cnaeCatalog = [];
  const CNAE_COLORS = ['#38bdf8','#22c55e','#f59e0b','#f472b6','#a78bfa','#fb7185','#14b8a6','#eab308','#60a5fa','#f97316'];

  document.addEventListener('DOMContentLoaded', () => {
    initMap();
    initChart();
    carregarCnaes();
    document.getElementById('detailDrawer')?.addEventListener('click', event => {
      if (event.target.id === 'detailDrawer') fecharDetalhe();
    });
  });

  /* â”€â”€â”€ STATUS â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
  function setStatus(msg, tipo = 'loading') {
    document.getElementById('statusText').textContent = msg;
    const dot = document.getElementById('statusDot');
    dot.className = 'status-dot ' + (tipo === 'loading' ? '' : tipo);
  }

  function supabasePronto() {
    if (dbClient) return true;
    setStatus('Erro: configure config.public.js com SUPABASE_URL e SUPABASE_ANON_KEY.', 'err');
    return false;
  }

  function initMap() {
    map = L.map('map', { zoomControl: true }).setView([-23.4205, -51.9331], 11);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; OpenStreetMap'
    }).addTo(map);
    markerLayer = L.layerGroup().addTo(map);
    selectedLayer = L.layerGroup().addTo(map);
  }

  function initChart() {
    const ctx = document.getElementById('cnaeChart');
    cnaeChart = new Chart(ctx, {
      type: 'bar',
      data: { labels: [], datasets: [{ data: [], backgroundColor: CNAE_COLORS }] },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        indexAxis: 'y',
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: ctx => `${ctx.raw} empresas` } }
        },
        scales: {
          x: { ticks: { color: '#94a3b8' }, grid: { color: 'rgba(148,163,184,.12)' } },
          y: { ticks: { color: '#e2e8f0', font: { size: 10 } }, grid: { display: false } }
        }
      }
    });
  }

  async function carregarCnaes() {
    if (!dbClient) return;
    const { data, error } = await dbClient
      .from('empresas')
      .select('cnae_principal, descricao_cnae')
      .not('cnae_principal', 'is', null)
      .limit(3000);

    if (error || !data) return;

    const mapa = new Map();
    data.forEach(row => {
      if (!row.cnae_principal) return;
      mapa.set(row.cnae_principal, row.descricao_cnae || '');
    });
    cnaeCatalog = [...mapa.entries()]
      .map(([codigo, descricao]) => ({ codigo, descricao }))
      .sort((a, b) => a.codigo.localeCompare(b.codigo));

    document.getElementById('cnaeOptions').innerHTML = cnaeCatalog
      .map(c => `<option value="${c.codigo} - ${escapeHtml(c.descricao)}"></option>`)
      .join('');
  }

  /* â”€â”€â”€ GEOCODE â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
  async function geocode(cidade, uf) {
    const url = new URL('https://nominatim.openstreetmap.org/search');
    url.searchParams.set('q', `${cidade}, ${uf}, Brasil`);
    url.searchParams.set('format', 'json');
    url.searchParams.set('limit', '1');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    const r = await fetch(url, {
      headers: { 'Accept': 'application/json' },
      signal: controller.signal
    }).finally(() => clearTimeout(timeout));
    if (!r.ok) throw new Error('Erro na geolocalização da cidade.');
    const d = await r.json();
    if (!d.length) throw new Error(`Cidade "${cidade}" não encontrada.`);
    return { lat: parseFloat(d[0].lat), lon: parseFloat(d[0].lon) };
  }

  /* â”€â”€â”€ BUSCA PRINCIPAL â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
  async function buscarEmpresas() {
    if (!supabasePronto()) return;
    const cidade = document.getElementById('cidade').value.trim();
    const uf     = document.getElementById('uf').value.trim();
    const raio   = Number(document.getElementById('raio').value);
    const limite = Number(document.getElementById('limite').value);
    const cnae   = document.getElementById('cnae').value.trim();

    if (!cidade || !raio || !limite) {
      setStatus('Preencha cidade, raio e limite.', 'err'); return;
    }

    document.getElementById('btnBuscar').disabled = true;
    setStatus('Consultando empresas por cidade…');

    try {
      let resultado = await buscarPorCidade(cidade, uf, cnae, limite);
      let fallbackCidade = true;
      allData = resultado;
      lastGeo = null;
      aplicarFiltro();
      atualizarKPIs(resultado);

      const comCoordenadas = resultado.filter(r =>
        Number.isFinite(Number(r.latitude)) && Number.isFinite(Number(r.longitude))
      );

      if (comCoordenadas.length) {
        const geo = await geocode(cidade, uf);
        lastGeo = geo;
        setStatus('Refinando por raio no Supabase…');
        const raioResultado = await buscarPorRaio(geo, raio, limite, cnae, cidade, uf);
        if (raioResultado.length) {
          resultado = raioResultado;
          fallbackCidade = false;
          allData = resultado;
          aplicarFiltro();
          atualizarKPIs(resultado);
        }
      }

      setStatus(
        fallbackCidade
          ? `Concluído — ${allData.length} empresas por cidade. Geocodifique para mapa/raio.`
          : `Concluído — ${allData.length} empresas encontradas.`,
        'ok'
      );

    } catch (e) {
      setStatus('Erro: ' + e.message, 'err');
      console.error(e);
    } finally {
      document.getElementById('btnBuscar').disabled = false;
    }
  }

  /* â”€â”€â”€ FILTROS POR CONTATO â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
  function setFiltro(tipo) {
    filtroAtivo = tipo;
    ['Todos','Email','Telefone','Ambos'].forEach(n => {
      document.getElementById('chip'+n).classList.remove('active');
    });
    const idMap = { todos:'chipTodos', email:'chipEmail', telefone:'chipTelefone', ambos:'chipAmbos' };
    document.getElementById(idMap[tipo]).classList.add('active');
    aplicarFiltro();
  }

  function aplicarFiltro() {
    let d = [...allData];
    if (filtroAtivo === 'email')    d = d.filter(r => r.email);
    if (filtroAtivo === 'telefone') d = d.filter(r => r.telefone);
    if (filtroAtivo === 'ambos')    d = d.filter(r => r.email && r.telefone);
    filteredData = d;
    page = 1;
    filtrarTabela();
  }

  /* â”€â”€â”€ FILTRO DE TEXTO â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
  function filtrarTabela() {
    const q = document.getElementById('searchFilter').value.toLowerCase();
    let d = filteredData;
    if (q) {
      d = d.filter(r =>
        (r.razao_social  || '').toLowerCase().includes(q) ||
        (r.nome_fantasia || '').toLowerCase().includes(q) ||
        (r.cnae_principal || '').toLowerCase().includes(q) ||
        (r.descricao_cnae || '').toLowerCase().includes(q) ||
        (r.cidade        || '').toLowerCase().includes(q) ||
        (r.bairro        || '').toLowerCase().includes(q) ||
        (r.cnpj          || '').includes(q)
      );
    }
    // Sort
    d.sort((a, b) => {
      let av = a[sortCol] ?? '', bv = b[sortCol] ?? '';
      if (typeof av === 'number') return sortDir === 'asc' ? av - bv : bv - av;
      return sortDir === 'asc'
        ? String(av).localeCompare(String(bv))
        : String(bv).localeCompare(String(av));
    });
    renderTabela(d);
    renderPaginacao(d.length);
    atualizarInsights(d);
    if (q && d.length === 1) focarEmpresa(String(d[0].cnpj || '').replace(/\D/g, ''));
  }

  async function buscarPorRaio(geo, raio, limite, cnae, cidade, uf) {
    let { data, error } = await dbClient.rpc('buscar_empresas_raio', {
      lat_input:         geo.lat,
      lon_input:         geo.lon,
      raio_km:           raio,
      limite_resultados: limite,
      cnae_input:        cnaeQueryValue(cnae)
    });

    if (error && error.message && error.message.includes('Could not find the function')) {
      const fallback = await dbClient.rpc('buscar_empresas_raio', {
        lat_input:         geo.lat,
        lon_input:         geo.lon,
        raio_km:           raio,
        limite_resultados: limite
      });
      data = fallback.data;
      error = fallback.error;
    }

    if (error) {
      console.warn('Busca por raio falhou, mantendo resultado por cidade:', error.message);
      return [];
    }

    return (data || []).filter(row =>
      normalizarCidade(row.cidade) === normalizarCidade(cidade) &&
      String(row.uf || '').trim().toUpperCase() === String(uf || '').trim().toUpperCase()
    );
  }

  async function buscarPorCidade(cidade, uf, cnae, limite) {
    let query = dbClient
      .from('empresas')
      .select('cnpj,razao_social,nome_fantasia,cnae_principal,descricao_cnae,cidade,uf,bairro,logradouro,numero,cep,telefone,email,latitude,longitude')
      .eq('cidade', normalizarCidade(cidade))
      .eq('uf', uf)
      .limit(limite);

    const cnaeValor = cnaeQueryValue(cnae);
    if (cnaeValor) {
      if (/^\d+$/.test(cnaeValor)) query = query.ilike('cnae_principal', `${cnaeValor}%`);
      else query = query.ilike('descricao_cnae', `%${normalizarBusca(cnaeValor)}%`);
    }

    const { data, error } = await query;
    if (error) throw new Error(error.message);
    return (data || []).map(row => ({ ...row, distancia_km: null }));
  }

  /* â”€â”€â”€ SORT â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
  function sortBy(col) {
    if (sortCol === col) sortDir = sortDir === 'asc' ? 'desc' : 'asc';
    else { sortCol = col; sortDir = 'asc'; }
    document.querySelectorAll('thead th').forEach(th => {
      th.classList.remove('sort-asc','sort-desc');
    });
    event.currentTarget.classList.add('sort-' + sortDir);
    filtrarTabela();
  }

  /* â”€â”€â”€ RENDER TABELA â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
  function renderTabela(dados) {
    const tbody = document.getElementById('tbody');
    const slice = dados.slice((page-1)*PAGE_SIZE, page*PAGE_SIZE);

    if (!slice.length) {
      tbody.innerHTML = `<tr class="empty-row"><td colspan="10">Nenhum resultado para os filtros selecionados.</td></tr>`;
      return;
    }

    tbody.innerHTML = slice.map(r => `
      <tr onclick="focarEmpresa('${String(r.cnpj || '').replace(/\D/g, '')}')">
        <td title="${r.cnpj}">${fmtCNPJ(r.cnpj)}</td>
        <td title="${r.razao_social || ''}">${r.razao_social || '—'}</td>
        <td title="${r.nome_fantasia || ''}">${r.nome_fantasia || '—'}</td>
        <td>${r.cnae_principal || '—'}</td>
        <td title="${r.descricao_cnae || ''}">${r.descricao_cnae || '—'}</td>
        <td>${r.cidade || '—'}</td>
        <td>${r.uf || '—'}</td>
        <td>${r.telefone
          ? `<span class="badge badge-phone">${r.telefone}</span>`
          : `<span class="badge badge-none">—</span>`}</td>
        <td title="${r.email || ''}">${r.email
          ? `<span class="badge badge-email">${r.email}</span>`
          : `<span class="badge badge-none">—</span>`}</td>
        <td>${r.distancia_km != null ? `<span class="dist-pill">${r.distancia_km} km</span>` : '—'}</td>
      </tr>
    `).join('');
  }

  /* â”€â”€â”€ PAGINAÇÃƒO â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
  function renderPaginacao(total) {
    const pages = Math.ceil(total / PAGE_SIZE);
    const pag   = document.getElementById('paginacao');
    if (pages <= 1) { pag.innerHTML = ''; return; }

    let html = '';
    if (page > 1) html += `<button class="page-btn" onclick="goPage(${page-1})">←</button>`;

    const start = Math.max(1, page-2), end = Math.min(pages, page+2);
    if (start > 1) html += `<button class="page-btn" onclick="goPage(1)">1</button>${start>2?'<span class="page-info">…</span>':''}`;
    for (let i=start; i<=end; i++) html += `<button class="page-btn ${i===page?'active':''}" onclick="goPage(${i})">${i}</button>`;
    if (end < pages) html += `${end<pages-1?'<span class="page-info">…</span>':''}<button class="page-btn" onclick="goPage(${pages})">${pages}</button>`;

    if (page < pages) html += `<button class="page-btn" onclick="goPage(${page+1})">→</button>`;
    html += `<span class="page-info">${total} resultados</span>`;
    pag.innerHTML = html;
  }

  function goPage(n) { page = n; filtrarTabela(); }

  function getCnaeKey(row) {
    return row.cnae_principal || 'SEM_CNAE';
  }

  function getCnaeLabel(row) {
    return row.descricao_cnae || row.cnae_principal || 'Sem CNAE';
  }

  function cnaeTop(dados, limite = 10) {
    const mapa = new Map();
    dados.forEach(row => {
      const key = getCnaeKey(row);
      if (!mapa.has(key)) {
        mapa.set(key, { key, label: getCnaeLabel(row), total: 0 });
      }
      mapa.get(key).total += 1;
    });
    return [...mapa.values()].sort((a, b) => b.total - a.total).slice(0, limite);
  }

  function colorForCnae(cnae, ranking) {
    const idx = ranking.findIndex(item => item.key === cnae);
    return CNAE_COLORS[idx >= 0 ? idx % CNAE_COLORS.length : CNAE_COLORS.length - 1];
  }

  function atualizarInsights(dados) {
    atualizarGraficoCnae(dados);
    atualizarMapa(dados);
  }

  function atualizarGraficoCnae(dados) {
    if (!cnaeChart) return;
    const top = cnaeTop(dados, 10);
    cnaeChart.data.labels = top.map(item => item.label.length > 38 ? item.label.slice(0, 38) + '...' : item.label);
    cnaeChart.data.datasets[0].data = top.map(item => item.total);
    cnaeChart.data.datasets[0].backgroundColor = top.map((_, idx) => CNAE_COLORS[idx % CNAE_COLORS.length]);
    cnaeChart.update();
  }

  function atualizarMapa(dados) {
    if (!map || !markerLayer) return;
    markerLayer.clearLayers();

    const comCoordenadas = dados
      .filter(row => Number.isFinite(Number(row.latitude)) && Number.isFinite(Number(row.longitude)))
      .slice(0, 1000);

    const top = cnaeTop(comCoordenadas, 10);
    const legend = document.getElementById('mapLegend');
    legend.innerHTML = top.slice(0, 5).map((item, idx) => `
      <span class="legend-item" title="${escapeHtml(item.label)}">
        <span class="legend-dot" style="background:${CNAE_COLORS[idx]}"></span>
        ${escapeHtml(item.label.length > 20 ? item.label.slice(0, 20) + '...' : item.label)}
      </span>
    `).join('');

    if (!comCoordenadas.length) {
      if (lastGeo) map.setView([lastGeo.lat, lastGeo.lon], 12);
      return;
    }

    const bounds = [];
    comCoordenadas.forEach(row => {
      const lat = Number(row.latitude);
      const lon = Number(row.longitude);
      const color = colorForCnae(getCnaeKey(row), top);
      const marker = L.circleMarker([lat, lon], {
        radius: 6,
        color,
        fillColor: color,
        fillOpacity: .82,
        weight: 1
      }).bindPopup(`
        <strong>${escapeHtml(row.nome_fantasia || row.razao_social || 'Empresa')}</strong><br>
        ${escapeHtml(row.descricao_cnae || row.cnae_principal || '')}<br>
        ${escapeHtml(row.logradouro || '')}, ${escapeHtml(row.numero || '')}<br>
        ${escapeHtml(row.bairro || '')} - ${escapeHtml(row.cidade || '')}
      `);
      marker.on('click', () => abrirDetalhe(row));
      markerLayer.addLayer(marker);
      bounds.push([lat, lon]);
    });

    if (bounds.length === 1) map.setView(bounds[0], 15);
    else map.fitBounds(bounds, { padding: [28, 28], maxZoom: 15 });
  }

  async function focarEmpresa(cnpj) {
    const empresa = allData.find(row => String(row.cnpj || '').replace(/\D/g, '') === cnpj);
    if (!empresa) return;

    let lat = Number(empresa.latitude);
    let lon = Number(empresa.longitude);

    if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
      setStatus('Localizando endereço da empresa...');
      const ponto = await geocodeEmpresa(empresa);
      if (!ponto) {
        setStatus('Não foi possível localizar o endereço desta empresa.', 'err');
        return;
      }
      lat = ponto.lat;
      lon = ponto.lon;
      empresa.latitude = lat;
      empresa.longitude = lon;
      setStatus('Endereço localizado no mapa. Rode a geocodificação no ETL para salvar definitivo.', 'ok');
    }

    selectedLayer.clearLayers();
    const marker = L.circleMarker([lat, lon], {
      radius: 11,
      color: '#ffffff',
      fillColor: '#22c55e',
      fillOpacity: .95,
      weight: 3
    }).bindPopup(`
      <strong>${escapeHtml(empresa.nome_fantasia || empresa.razao_social || 'Empresa')}</strong><br>
      ${escapeHtml(empresa.descricao_cnae || empresa.cnae_principal || '')}<br>
      ${escapeHtml(empresa.logradouro || '')}, ${escapeHtml(empresa.numero || '')}<br>
      ${escapeHtml(empresa.bairro || '')} - ${escapeHtml(empresa.cidade || '')}
    `);
    selectedLayer.addLayer(marker);
    map.setView([lat, lon], 17);
    marker.openPopup();
    abrirDetalhe(empresa);
  }

  async function geocodeEmpresa(empresa) {
    const endereco = [
      empresa.logradouro,
      empresa.numero,
      empresa.bairro,
      empresa.cidade,
      empresa.uf,
      empresa.cep,
      'Brasil'
    ].filter(Boolean).join(', ');

    if (!endereco.trim()) return null;

    const url = new URL('https://nominatim.openstreetmap.org/search');
    url.searchParams.set('q', endereco);
    url.searchParams.set('format', 'json');
    url.searchParams.set('limit', '1');
    url.searchParams.set('countrycodes', 'br');

    const r = await fetch(url, { headers: { 'Accept': 'application/json' } });
    if (!r.ok) return null;
    const data = await r.json();
    if (!data.length) return null;
    return { lat: Number(data[0].lat), lon: Number(data[0].lon) };
  }

  /* â”€â”€â”€ KPIs â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
  function atualizarKPIs(dados) {
    const total   = dados.length;
    const emails  = dados.filter(r => r.email).length;
    const tels    = dados.filter(r => r.telefone).length;
    const media   = total
      ? (dados.reduce((s, r) => s + Number(r.distancia_km || 0), 0) / total).toFixed(1)
      : 0;

    document.getElementById('kpiTotal').textContent = total.toLocaleString('pt-BR');
    document.getElementById('kpiEmail').textContent = emails.toLocaleString('pt-BR');
    document.getElementById('kpiTel').textContent   = tels.toLocaleString('pt-BR');
    document.getElementById('kpiDist').textContent  = media + ' km';
  }

  /* â”€â”€â”€ EXPORT CSV â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
  function baixarCSV() {
    if (!allData.length) { alert('Nenhum dado para exportar.'); return; }

    const cols = ['cnpj','razao_social','nome_fantasia','cnae_principal','descricao_cnae',
                  'cidade','uf','bairro','logradouro','numero','cep','telefone','email',
                  'latitude','longitude','distancia_km'];

    const rows = [
      cols.join(';'),
      ...allData.map(r => cols.map(c => limparCSV(r[c])).join(';'))
    ];

    const blob = new Blob(['\ufeff' + rows.join('\n')], { type:'text/csv;charset=utf-8;' });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href     = url;
    a.download = `cnpj_${document.getElementById('cidade').value}_${Date.now()}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  function limparCSV(v) {
    if (v == null) return '';
    return String(v).replace(/;/g,' ').replace(/[\r\n]/g,' ').trim();
  }

  function abrirDetalhe(empresa) {
    const drawer = document.getElementById('detailDrawer');
    const body = document.getElementById('detailBody');
    const endereco = [
      empresa.logradouro,
      empresa.numero,
      empresa.bairro,
      empresa.cidade,
      empresa.uf,
      empresa.cep
    ].filter(Boolean).join(', ');
    const hasCoords = Number.isFinite(Number(empresa.latitude)) && Number.isFinite(Number(empresa.longitude));
    const mapsUrl = hasCoords
      ? `https://www.google.com/maps?q=${empresa.latitude},${empresa.longitude}`
      : `https://www.google.com/maps/search/${encodeURIComponent(endereco)}`;
    const osmUrl = hasCoords
      ? `https://www.openstreetmap.org/?mlat=${empresa.latitude}&mlon=${empresa.longitude}#map=18/${empresa.latitude}/${empresa.longitude}`
      : `https://www.openstreetmap.org/search?query=${encodeURIComponent(endereco)}`;

    body.innerHTML = `
      <div>
        <h2 class="detail-title">${escapeHtml(empresa.nome_fantasia || empresa.razao_social || 'Empresa sem nome')}</h2>
        <p class="detail-subtitle">${fmtCNPJ(empresa.cnpj)} · ${escapeHtml(empresa.cidade || '')}/${escapeHtml(empresa.uf || '')}</p>
      </div>
      <div class="detail-grid">
        <div class="detail-item">
          <span class="detail-label">Razão social</span>
          <span class="detail-value">${escapeHtml(empresa.razao_social || '—')}</span>
        </div>
        <div class="detail-item">
          <span class="detail-label">Atividade</span>
          <span class="detail-value">${escapeHtml(empresa.cnae_principal || '—')} · ${escapeHtml(empresa.descricao_cnae || '—')}</span>
        </div>
        <div class="detail-item">
          <span class="detail-label">Endereço</span>
          <span class="detail-value">${escapeHtml(endereco || '—')}</span>
        </div>
        <div class="detail-item">
          <span class="detail-label">Contato</span>
          <span class="detail-value">${escapeHtml(empresa.telefone || '—')}<br>${escapeHtml(empresa.email || '—')}</span>
        </div>
        <div class="detail-item">
          <span class="detail-label">Distância</span>
          <span class="detail-value">${empresa.distancia_km != null ? `${empresa.distancia_km} km` : 'Sem cálculo por raio'}</span>
        </div>
      </div>
      <div class="detail-actions">
        <button class="btn btn-outline" type="button" onclick="copiarTexto('${escapeAttr(empresa.cnpj || '')}')">Copiar CNPJ</button>
        <a class="btn btn-outline" href="${mapsUrl}" target="_blank" rel="noopener">Google Maps</a>
        <a class="btn btn-outline" href="${osmUrl}" target="_blank" rel="noopener">OpenStreetMap</a>
      </div>
    `;
    drawer.classList.add('open');
    drawer.setAttribute('aria-hidden', 'false');
  }

  function fecharDetalhe() {
    const drawer = document.getElementById('detailDrawer');
    drawer.classList.remove('open');
    drawer.setAttribute('aria-hidden', 'true');
  }

  function copiarTexto(texto) {
    navigator.clipboard?.writeText(texto);
    setStatus('Copiado para a área de transferência.', 'ok');
  }

  function escapeHtml(v) {
    return String(v ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function escapeAttr(v) {
    return String(v ?? '').replace(/\\/g, '\\\\').replace(/'/g, "\\'");
  }

  function cnaeQueryValue(valor) {
    return String(valor || '').split(' - ')[0].trim();
  }

  function normalizarCidade(valor) {
    return String(valor || '')
      .normalize('NFD')
      .replace(/[\u0300-\u036f]/g, '')
      .trim()
      .toUpperCase();
  }

  function normalizarBusca(valor) {
    return String(valor || '')
      .normalize('NFD')
      .replace(/[\u0300-\u036f]/g, '')
      .trim()
      .toUpperCase();
  }

  function matchesCnae(row, valor) {
    const q = normalizarBusca(cnaeQueryValue(valor)).toLowerCase();
    if (!q) return true;
    const codigo = normalizarBusca(row.cnae_principal).toLowerCase();
    const descricao = normalizarBusca(row.descricao_cnae).toLowerCase();
    return codigo.startsWith(q) || descricao.includes(q);
  }

  function fmtCNPJ(cnpj) {
    if (!cnpj) return '';
    const v = String(cnpj).replace(/\D/g,'').padStart(14,'0');
    return v.replace(/^(\d{2})(\d{3})(\d{3})(\d{4})(\d{2})$/, '$1.$2.$3/$4-$5');
  }

  /* â”€â”€â”€ LIMPAR â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
  function limpar() {
    allData = filteredData = [];
    document.getElementById('tbody').innerHTML =
      `<tr class="empty-row"><td colspan="10">Faça uma busca para visualizar os resultados.</td></tr>`;
    document.getElementById('paginacao').innerHTML = '';
    atualizarInsights([]);
    atualizarKPIs([]);
    ['kpiTotal','kpiEmail','kpiTel','kpiDist'].forEach(id => document.getElementById(id).textContent = '—');
    setStatus('Aguardando busca...', 'idle');
  }

  Object.assign(window, {
    baixarCSV,
    buscarEmpresas,
    copiarTexto,
    fecharDetalhe,
    filtrarTabela,
    focarEmpresa,
    goPage,
    limpar,
    setFiltro,
    sortBy
  });
})();

