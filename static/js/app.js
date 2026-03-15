const $ = (id) => document.getElementById(id);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  me: null,
  currentTab: 'dashboard',
  editingId: null,
  editingOriginalTipo: 'GASTO',
  charts: {},
  dashboardMonth: null,
  dashboardYear: null,
  latestLancamentos: [],
  latestInvestimentos: [],
  latestOrcamentos: [],
};

const monthNames = [
  'Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
  'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro'
];

function nowLocalDateInputValue() {
  const d = new Date();
  const offsetMs = d.getTimezoneOffset() * 60000;
  return new Date(d.getTime() - offsetMs).toISOString().slice(0, 10);
}

function parseErrorMessage(data, fallback) {
  if (!data) return fallback;
  return data.error || data.message || fallback;
}

async function api(path, method = 'GET', body = null) {
  const options = {
    method,
    credentials: 'include',
    headers: {},
  };

  if (body !== null) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }

  const res = await fetch(path, options);
  const contentType = res.headers.get('content-type') || '';

  let data = null;
  if (contentType.includes('application/json')) {
    data = await res.json();
  } else {
    const text = await res.text();
    data = text ? { message: text } : null;
  }

  if (!res.ok) {
    throw new Error(parseErrorMessage(data, `Erro ${res.status}`));
  }

  return data;
}

function fmtBRL(value) {
  return new Intl.NumberFormat('pt-BR', {
    style: 'currency',
    currency: 'BRL',
  }).format(Number(value || 0));
}

function escapeHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function showToast(el, type, title, desc = '') {
  if (!el) return;
  el.className = `toast show ${type === 'ok' ? 'ok' : type === 'warn' ? 'warn' : 'err'}`;
  el.innerHTML = `<div class="t">${escapeHtml(title)}</div>${desc ? `<div class="d">${escapeHtml(desc)}</div>` : ''}`;
}

function hideToast(el) {
  if (!el) return;
  el.className = 'toast';
  el.innerHTML = '';
}

function setButtonLoading(btn, loading, label = 'Salvando...') {
  if (!btn) return;
  if (loading) {
    btn.dataset.originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = label;
  } else {
    btn.disabled = false;
    btn.textContent = btn.dataset.originalText || btn.textContent;
  }
}

function buildMonthSelect() {
  const sel = $('dashMes');
  if (!sel) return;
  sel.innerHTML = monthNames
    .map((name, idx) => `<option value="${idx + 1}">${name}</option>`)
    .join('');
}

function setDefaultPeriod() {
  const now = new Date();
  state.dashboardMonth = now.getMonth() + 1;
  state.dashboardYear = now.getFullYear();
  if ($('dashMes')) $('dashMes').value = String(state.dashboardMonth);
  if ($('dashAno')) $('dashAno').value = String(state.dashboardYear);
  if ($('orcMes')) $('orcMes').value = String(state.dashboardMonth);
  if ($('orcAno')) $('orcAno').value = String(state.dashboardYear);
  if ($('newOrcMes')) $('newOrcMes').value = String(state.dashboardMonth);
  if ($('newOrcAno')) $('newOrcAno').value = String(state.dashboardYear);
}

function getSelectedPeriod() {
  const mes = Number(($('dashMes')?.value || state.dashboardMonth || 1));
  const ano = Number(($('dashAno')?.value || state.dashboardYear || new Date().getFullYear()));
  return { mes, ano };
}

function tabSectionId(tab) {
  return `tab-${tab}`;
}

function setTab(tab) {
  state.currentTab = tab;
  $$('.tab').forEach((el) => {
    el.classList.toggle('active', el.dataset.tab === tab);
  });
  ['dashboard', 'lancar', 'ultimos', 'investimentos', 'orcamentos', 'assistente'].forEach((name) => {
    const section = $(tabSectionId(name));
    if (!section) return;
    section.classList.toggle('hidden', name !== tab);
  });
}

function setupTabs() {
  $$('.tab').forEach((el) => {
    el.addEventListener('click', () => setTab(el.dataset.tab));
  });
}

function toggleAccountMenu(force) {
  const dropdown = $('accountDropdown');
  if (!dropdown) return;
  const open = typeof force === 'boolean' ? force : !dropdown.classList.contains('show');
  dropdown.classList.toggle('show', open);
}

function toggleFab(force) {
  const wrap = $('fabWrap');
  const backdrop = $('fabBackdrop');
  if (!wrap || !backdrop) return;
  const open = typeof force === 'boolean' ? force : !wrap.classList.contains('open');
  wrap.classList.toggle('open', open);
  backdrop.classList.toggle('show', open);
}

function updateAccountUI(me) {
  const logged = Boolean(me && me.user_id);
  const name = me?.name || 'Sua conta';
  const email = me?.email || 'Não conectado';

  $('accountName').textContent = name;
  $('accountEmail').textContent = email;
  if ($('accountNameDropdown')) $('accountNameDropdown').textContent = name;
  if ($('accountEmailDropdown')) $('accountEmailDropdown').textContent = email;

  $('btnLogin')?.classList.toggle('hidden', logged);
  $('btnEditConta')?.classList.toggle('hidden', !logged);
  $('btnLogout')?.classList.toggle('hidden', !logged);
  $('helloTitle').textContent = logged ? `Olá, ${me.name || me.email || 'usuário'} 👋` : 'Olá 👋';
}

async function loadMe() {
  const me = await api('/api/me');
  state.me = me;
  updateAccountUI(me);
  return me;
}

async function logout() {
  await api('/api/logout', 'POST');
  window.location.href = '/login';
}

function openEditModal(item) {
  state.editingId = item.id;
  state.editingOriginalTipo = item.tipo || 'GASTO';
  $('modalTitle').textContent = `Editar lançamento #${item.id}`;
  $('edtData').value = item.data || nowLocalDateInputValue();
  $('edtCategoria').value = item.categoria || '';
  $('edtValor').value = Number(item.valor || 0);
  $('edtDescricao').value = item.descricao || '';
  $('overlay').classList.add('show');
  hideToast($('toastEdit'));
}

function closeEditModal() {
  state.editingId = null;
  $('overlay').classList.remove('show');
  hideToast($('toastEdit'));
}

function destroyChart(name) {
  if (state.charts[name]) {
    state.charts[name].destroy();
    state.charts[name] = null;
  }
}

function makeChart(name, canvasId, config) {
  const el = $(canvasId);
  if (!el || typeof Chart === 'undefined') return;
  destroyChart(name);
  state.charts[name] = new Chart(el, config);
}

async function loadDashboard() {
  const toast = $('toastDash');
  hideToast(toast);

  try {
    const { mes, ano } = getSelectedPeriod();
    state.dashboardMonth = mes;
    state.dashboardYear = ano;

    const [dash, insights, proj, patr] = await Promise.all([
      api(`/api/dashboard?mes=${mes}&ano=${ano}`),
      api(`/api/insights_dashboard?mes=${mes}&ano=${ano}`),
      api('/api/projecao'),
      api('/api/patrimonio?months=6'),
    ]);

    const receitasFmt = fmtBRL(dash.receitas);
    const gastosFmt = fmtBRL(dash.gastos);
    const saldoFmt = fmtBRL(dash.saldo);

    $('valReceitas').textContent = receitasFmt;
    $('valGastos').textContent = gastosFmt;
    $('valSaldo').textContent = saldoFmt;
    if ($('statReceitasMirror')) $('statReceitasMirror').textContent = receitasFmt;
    if ($('statGastosMirror')) $('statGastosMirror').textContent = gastosFmt;
    if ($('statSaldoMirror')) $('statSaldoMirror').textContent = saldoFmt;

    const insightText = insights?.insight || 'Seu resumo financeiro está disponível.';
    const projected = proj?.saldo_previsto != null ? ` Saldo previsto do mês: ${fmtBRL(proj.saldo_previsto)}.` : '';
    $('helloSub').textContent = `${insightText}${projected}`;

    makeChart('rg', 'chartRG', {
      type: 'bar',
      data: {
        labels: ['Receitas', 'Gastos'],
        datasets: [{ label: 'Valores', data: [Number(dash.receitas || 0), Number(dash.gastos || 0)] }],
      },
      options: { responsive: true, maintainAspectRatio: false },
    });

    makeChart('saldo', 'chartSaldo', {
      type: 'doughnut',
      data: {
        labels: ['Saldo', 'Gastos'],
        datasets: [{ data: [Math.max(Number(dash.saldo || 0), 0), Number(dash.gastos || 0)] }],
      },
      options: { responsive: true, maintainAspectRatio: false },
    });

    makeChart('daily', 'chartDaily', {
      type: 'bar',
      data: {
        labels: ['Gasto médio diário', 'Gastos restantes'],
        datasets: [{ label: 'Projeção', data: [Number(proj.gasto_medio_diario || 0), Number(proj.estimativa_gastos_restantes || 0)] }],
      },
      options: { responsive: true, maintainAspectRatio: false },
    });

    makeChart('patrimonio', 'chartPatrimonio', {
      type: 'line',
      data: {
        labels: patr.labels || [],
        datasets: [{ label: 'Patrimônio', data: patr.values || [] }],
      },
      options: { responsive: true, maintainAspectRatio: false },
    });

    makeChart('categorias', 'chartCategorias', {
      type: 'pie',
      data: {
        labels: insights.categorias || ['Sem dados'],
        datasets: [{ data: (insights.valores && insights.valores.length ? insights.valores : [1]) }],
      },
      options: { responsive: true, maintainAspectRatio: false },
    });
  } catch (e) {
    showToast(toast, 'err', 'Erro no dashboard', e.message);
  }
}

function resetLancamentoForm() {
  $('lanTipo').value = 'GASTO';
  $('lanData').value = nowLocalDateInputValue();
  $('lanCategoria').value = '';
  $('lanValor').value = '';
  $('lanDescricao').value = '';
}

async function saveLancamento() {
  const toast = $('toastLanc');
  hideToast(toast);
  const btn = $('btnSalvarLanc');
  setButtonLoading(btn, true);

  try {
    const payload = {
      tipo: $('lanTipo').value,
      data: $('lanData').value,
      categoria: $('lanCategoria').value,
      valor: $('lanValor').value,
      descricao: $('lanDescricao').value,
    };

    if (!payload.data) throw new Error('Informe a data.');
    if (!payload.valor) throw new Error('Informe o valor.');

    await api('/api/lancamentos', 'POST', payload);
    showToast(toast, 'ok', 'Lançamento salvo', 'Seu lançamento foi registrado com sucesso.');
    resetLancamentoForm();
    await Promise.all([loadDashboard(), loadUltimos()]);
    setTab('ultimos');
  } catch (e) {
    showToast(toast, 'err', 'Falha ao salvar', e.message);
  } finally {
    setButtonLoading(btn, false);
  }
}

function renderUltimos(items) {
  const root = $('listaUltimos');
  if (!root) return;

  if (!items.length) {
    root.innerHTML = '<div class="emptyState">Nenhum lançamento cadastrado ainda.</div>';
    return;
  }

  root.innerHTML = items.map((item) => `
    <div class="miniCard">
      <div class="row" style="align-items:flex-start; gap:12px;">
        <div>
          <div><strong>${escapeHtml(item.descricao || 'Sem descrição')}</strong></div>
          <div class="muted">${escapeHtml(item.categoria || 'Sem categoria')} • ${escapeHtml(item.data || '')}</div>
          <div class="muted">Tipo: ${escapeHtml(item.tipo || '')}</div>
        </div>
        <div style="text-align:right; margin-left:auto;">
          <div><strong>${fmtBRL(item.valor)}</strong></div>
          <div class="row" style="gap:8px; justify-content:flex-end; margin-top:8px;">
            <button class="btn" data-action="edit-lanc" data-id="${item.id}">Editar</button>
            <button class="btn danger" data-action="delete-lanc" data-id="${item.id}">Excluir</button>
          </div>
        </div>
      </div>
    </div>
  `).join('');
}

async function loadUltimos() {
  const toast = $('toastUltimos');
  hideToast(toast);

  try {
    const data = await api('/api/lancamentos?limit=30');
    state.latestLancamentos = Array.isArray(data.items) ? data.items : [];
    renderUltimos(state.latestLancamentos);
  } catch (e) {
    renderUltimos([]);
    showToast(toast, 'err', 'Erro ao carregar', e.message);
  }
}

async function saveEditLancamento() {
  const toast = $('toastEdit');
  hideToast(toast);
  const btn = $('btnSalvarEdicao');
  setButtonLoading(btn, true);

  try {
    if (!state.editingId) throw new Error('Nenhum lançamento selecionado.');

    await api(`/api/lancamentos/${state.editingId}`, 'PUT', {
      tipo: state.editingOriginalTipo,
      data: $('edtData').value,
      categoria: $('edtCategoria').value,
      valor: $('edtValor').value,
      descricao: $('edtDescricao').value,
    });

    showToast(toast, 'ok', 'Lançamento atualizado');
    await Promise.all([loadDashboard(), loadUltimos()]);
    setTimeout(closeEditModal, 500);
  } catch (e) {
    showToast(toast, 'err', 'Erro ao atualizar', e.message);
  } finally {
    setButtonLoading(btn, false);
  }
}

async function deleteLancamento(id) {
  const toast = $('toastUltimos');
  hideToast(toast);

  if (!window.confirm('Deseja realmente excluir este lançamento?')) return;

  try {
    await api(`/api/lancamentos/${id}`, 'DELETE');
    showToast(toast, 'ok', 'Lançamento excluído');
    await Promise.all([loadDashboard(), loadUltimos(), loadOrcamentos()]);
  } catch (e) {
    showToast(toast, 'err', 'Erro ao excluir', e.message);
  }
}

function resetInvestimentoForm() {
  $('invData').value = nowLocalDateInputValue();
  $('invAtivo').value = '';
  $('invTipo').value = 'APORTE';
  $('invValor').value = '';
  $('invDescricao').value = '';
}

function renderInvestimentos(items) {
  const root = $('listaInv');
  if (!root) return;
  if (!items.length) {
    root.innerHTML = '<div class="emptyState">Nenhum investimento registrado ainda.</div>';
    return;
  }

  root.innerHTML = items.map((item) => `
    <div class="miniCard">
      <div class="row" style="align-items:flex-start; gap:12px;">
        <div>
          <div><strong>${escapeHtml(item.ativo || 'Ativo')}</strong></div>
          <div class="muted">${escapeHtml(item.tipo || '')} • ${escapeHtml(item.data || '')}</div>
          <div class="muted">${escapeHtml(item.descricao || '')}</div>
        </div>
        <div style="text-align:right; margin-left:auto;">
          <div><strong>${fmtBRL(item.valor)}</strong></div>
          <button class="btn danger" data-action="delete-inv" data-id="${item.id}" style="margin-top:8px;">Excluir</button>
        </div>
      </div>
    </div>
  `).join('');
}

async function loadInvestimentos() {
  const toast = $('toastInv');
  hideToast(toast);

  try {
    const data = await api('/api/investimentos?limit=50');
    state.latestInvestimentos = Array.isArray(data.items) ? data.items : [];
    renderInvestimentos(state.latestInvestimentos);
  } catch (e) {
    renderInvestimentos([]);
    showToast(toast, 'err', 'Erro ao carregar investimentos', e.message);
  }
}

async function saveInvestimento() {
  const toast = $('toastInv');
  hideToast(toast);
  const btn = $('btnSalvarInv');
  setButtonLoading(btn, true);

  try {
    await api('/api/investimentos', 'POST', {
      data: $('invData').value,
      ativo: $('invAtivo').value,
      tipo: $('invTipo').value,
      valor: $('invValor').value,
      descricao: $('invDescricao').value,
    });

    showToast(toast, 'ok', 'Investimento salvo');
    resetInvestimentoForm();
    await Promise.all([loadInvestimentos(), loadDashboard()]);
  } catch (e) {
    showToast(toast, 'err', 'Erro ao salvar investimento', e.message);
  } finally {
    setButtonLoading(btn, false, 'Salvar investimento');
  }
}

async function deleteInvestimento(id) {
  const toast = $('toastInv');
  hideToast(toast);
  if (!window.confirm('Excluir este investimento?')) return;

  try {
    await api(`/api/investimentos/${id}`, 'DELETE');
    showToast(toast, 'ok', 'Investimento excluído');
    await Promise.all([loadInvestimentos(), loadDashboard()]);
  } catch (e) {
    showToast(toast, 'err', 'Erro ao excluir investimento', e.message);
  }
}

function fillBudgetPeriodInputs() {
  if (!$('orcMes') || !$('orcAno')) return;
  $('orcMes').value = String(state.dashboardMonth);
  $('orcAno').value = String(state.dashboardYear);
}

function renderOrcamentos(items) {
  const root = $('listaOrcamentos');
  if (!root) return;

  if (!items.length) {
    root.innerHTML = '<div class="emptyState">Nenhum orçamento cadastrado para este período.</div>';
    return;
  }

  root.innerHTML = items.map((item) => {
    const pct = Number(item.percentual || 0);
    const width = Math.max(0, Math.min(100, pct));
    return `
      <div class="miniCard">
        <div class="row" style="gap:12px; align-items:flex-start;">
          <div>
            <div><strong>${escapeHtml(item.categoria || 'Categoria')}</strong></div>
            <div class="muted">Meta: ${fmtBRL(item.valor_meta)} • Gasto atual: ${fmtBRL(item.gasto_atual)}</div>
            <div class="muted">Status: ${escapeHtml(item.status || 'ok')} ${item.mensagem ? `• ${escapeHtml(item.mensagem)}` : ''}</div>
          </div>
          <div style="margin-left:auto; text-align:right;">
            <div><strong>${Number.isFinite(pct) ? pct.toFixed(0) : '0'}%</strong></div>
            <button class="btn danger" data-action="delete-orc" data-id="${item.id}" style="margin-top:8px;">Excluir</button>
          </div>
        </div>
        <div class="orc-progress"><div style="width:${width}%;"></div></div>
      </div>
    `;
  }).join('');
}

async function loadOrcamentos() {
  const toast = $('toastOrc');
  hideToast(toast);

  try {
    const mes = Number($('orcMes')?.value || state.dashboardMonth);
    const ano = Number($('orcAno')?.value || state.dashboardYear);
    const data = await api(`/api/orcamentos?mes=${mes}&ano=${ano}`);
    state.latestOrcamentos = Array.isArray(data.items) ? data.items : [];
    renderOrcamentos(state.latestOrcamentos);
  } catch (e) {
    renderOrcamentos([]);
    showToast(toast, 'err', 'Erro ao carregar orçamentos', e.message);
  }
}

async function saveOrcamento() {
  const toast = $('toastOrc');
  hideToast(toast);
  const btn = $('btnSalvarOrc');
  setButtonLoading(btn, true);

  try {
    await api('/api/orcamentos', 'POST', {
      categoria: $('newOrcCategoria').value,
      valor_meta: $('newOrcValor').value,
      mes: $('newOrcMes').value,
      ano: $('newOrcAno').value,
    });

    showToast(toast, 'ok', 'Orçamento salvo');
    $('newOrcCategoria').value = '';
    $('newOrcValor').value = '';
    await loadOrcamentos();
  } catch (e) {
    showToast(toast, 'err', 'Erro ao salvar orçamento', e.message);
  } finally {
    setButtonLoading(btn, false, 'Salvar orçamento');
  }
}

async function deleteOrcamento(id) {
  const toast = $('toastOrc');
  hideToast(toast);
  if (!window.confirm('Excluir este orçamento?')) return;

  try {
    await api(`/api/orcamentos/${id}`, 'DELETE');
    showToast(toast, 'ok', 'Orçamento excluído');
    await loadOrcamentos();
  } catch (e) {
    showToast(toast, 'err', 'Erro ao excluir orçamento', e.message);
  }
}

async function askAssistant() {
  const toast = $('toastAssistente');
  hideToast(toast);
  const btn = $('btnPerguntarAssistente');
  const answerEl = $('assistantAnswer');
  setButtonLoading(btn, true, 'Perguntando...');

  try {
    const pergunta = String($('assistantQuestion').value || '').trim();
    if (!pergunta) throw new Error('Digite uma pergunta para o assistente.');

    const data = await api('/api/assistant_finance', 'POST', { pergunta });
    answerEl.innerHTML = `<div class="miniCard"><strong>Resposta:</strong><div style="margin-top:8px; white-space:pre-wrap;">${escapeHtml(data.resposta || 'Sem resposta.')}</div></div>`;
  } catch (e) {
    showToast(toast, 'err', 'Erro no assistente', e.message);
  } finally {
    setButtonLoading(btn, false, 'Perguntar');
  }
}

function bindStaticEvents() {
  $('btnAccount')?.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleAccountMenu();
  });

  document.addEventListener('click', (e) => {
    if (!e.target.closest('.accountMenuWrap')) toggleAccountMenu(false);
  });

  $('btnLogout')?.addEventListener('click', logout);
  $('btnEditConta')?.addEventListener('click', async () => {
    const current = state.me?.name || '';
    const novoNome = window.prompt('Digite o nome que deseja exibir na conta:', current);
    if (novoNome === null) return;

    try {
      const updated = await api('/api/account', 'POST', { name: novoNome });
      state.me = updated;
      updateAccountUI(updated);
    } catch (e) {
      alert(`Não foi possível atualizar a conta: ${e.message}`);
    }
  });

  $('btnAtualizarDash')?.addEventListener('click', async () => {
    await Promise.all([loadDashboard(), loadOrcamentos()]);
  });

  $('btnSalvarLanc')?.addEventListener('click', saveLancamento);
  $('btnLimparLanc')?.addEventListener('click', resetLancamentoForm);
  $('btnRecarregarUltimos')?.addEventListener('click', loadUltimos);
  $('btnRecarregarInv')?.addEventListener('click', loadInvestimentos);
  $('btnSalvarInv')?.addEventListener('click', saveInvestimento);
  $('btnSalvarOrc')?.addEventListener('click', saveOrcamento);
  $('btnRecarregarOrc')?.addEventListener('click', loadOrcamentos);
  $('btnPerguntarAssistente')?.addEventListener('click', askAssistant);

  $('btnSalvarEdicao')?.addEventListener('click', saveEditLancamento);
  $('btnCancelarEdicao')?.addEventListener('click', closeEditModal);
  $('btnCancelarEdicaoTop')?.addEventListener('click', closeEditModal);
  $('overlay')?.addEventListener('click', (e) => {
    if (e.target.id === 'overlay') closeEditModal();
  });

  $('fabMain')?.addEventListener('click', () => toggleFab());
  $('fabBackdrop')?.addEventListener('click', () => toggleFab(false));
  $('fabNovo')?.addEventListener('click', (e) => {
    e.preventDefault();
    toggleFab(false);
    setTab('lancar');
  });
  $('fabDash')?.addEventListener('click', (e) => {
    e.preventDefault();
    toggleFab(false);
    setTab('dashboard');
  });

  $('fabWhats')?.setAttribute(
    'href',
    `https://wa.me/5537998675231?text=${encodeURIComponent('Olá! Quero conectar meu e-mail no Finance AI.')}`
  );

  $('listaUltimos')?.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-action]');
    if (!btn) return;
    const id = Number(btn.dataset.id);
    if (!id) return;

    if (btn.dataset.action === 'edit-lanc') {
      const item = state.latestLancamentos.find((x) => Number(x.id) === id);
      if (item) openEditModal(item);
    }
    if (btn.dataset.action === 'delete-lanc') deleteLancamento(id);
  });

  $('listaInv')?.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-action="delete-inv"]');
    if (!btn) return;
    deleteInvestimento(Number(btn.dataset.id));
  });

  $('listaOrcamentos')?.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-action="delete-orc"]');
    if (!btn) return;
    deleteOrcamento(Number(btn.dataset.id));
  });
}

function setupFormDefaults() {
  $('lanData').value = nowLocalDateInputValue();
  $('edtData').setAttribute('type', 'date');
  $('edtValor').setAttribute('type', 'number');
  $('edtValor').setAttribute('step', '0.01');
  $('dashAno').setAttribute('type', 'number');
  $('lanValor').setAttribute('type', 'number');
  $('lanValor').setAttribute('step', '0.01');
  $('invData').value = nowLocalDateInputValue();
  $('invValor').setAttribute('type', 'number');
  $('invValor').setAttribute('step', '0.01');
  $('newOrcValor').setAttribute('type', 'number');
  $('newOrcValor').setAttribute('step', '0.01');
}

async function bootstrap() {
  try {
    setupTabs();
    buildMonthSelect();
    setDefaultPeriod();
    fillBudgetPeriodInputs();
    setupFormDefaults();
    bindStaticEvents();
    setTab('dashboard');

    const me = await loadMe();
    if (!me?.user_id) {
      window.location.href = '/login';
      return;
    }

    await Promise.all([
      loadDashboard(),
      loadUltimos(),
      loadInvestimentos(),
      loadOrcamentos(),
    ]);
  } catch (e) {
    alert(`Erro ao iniciar o app: ${e.message}`);
  }
}

bootstrap();
