const $ = (id) => document.getElementById(id);

const moneyBR = (n) => {
  const v = Number(n || 0);
  return v.toLocaleString("pt-BR", { style: "currency", currency: "BRL" });
};

async function api(path, method = "GET", body = null) {
  const opt = {
    method,
    headers: { "Content-Type": "application/json" },
    credentials: "include"
  };
  if (body) opt.body = JSON.stringify(body);

  const res = await fetch(path, opt);
  let data = null;
  try { data = await res.json(); } catch (e) {}

  if (!res.ok) {
    const msg = (data && (data.error || data.message))
      ? (data.error || data.message)
      : `Erro ${res.status}`;
    throw new Error(msg);
  }

  return data;
}

function showToast(el, type, title, desc = "") {
  if (!el) return;
  el.className = "toast show " + (type === "ok" ? "ok" : type === "warn" ? "warn" : "err");
  el.innerHTML = `<div class="t">${title}</div>` + (desc ? `<div class="d">${desc}</div>` : "");
}

function hideToast(el) {
  if (!el) return;
  el.className = "toast";
  el.innerHTML = "";
}

function isoToBR(iso) {
  if (!iso) return "";
  const s = String(iso).slice(0, 10);
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return s;
  return `${m[3]}-${m[2]}-${m[1]}`;
}

function brToISO(br) {
  if (!br) return "";
  const s = String(br).trim();
  const m = s.match(/^(\d{2})[\/-](\d{2})[\/-](\d{4})$/);
  if (!m) return s;
  const dd = m[1], mm = m[2], yyyy = m[3];
  return `${yyyy}-${mm}-${dd}`;
}

function firstNameFromEmail(email = "") {
  const raw = String(email || "").trim();
  if (!raw) return "você";
  const base = raw.split("@")[0] || "";
  const clean = base.split(/[._\-0-9]+/)[0] || base;
  if (!clean) return "você";
  return clean.charAt(0).toUpperCase() + clean.slice(1);
}

function greetingByHour() {
  const h = new Date().getHours();
  if (h < 12) return "Bom dia";
  if (h < 18) return "Boa tarde";
  return "Boa noite";
}

function refreshGreeting(nameOrEmail = "") {
  const raw = String(nameOrEmail || "").trim();
  const name = raw.includes("@") ? firstNameFromEmail(raw) : (raw || "você");
  $("helloTitle").textContent = `${greetingByHour()}, ${name} 👋`;
}

function escapeHtml(v) {
  return String(v ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

const WA_NUMBER_FALLBACK = "5537998675231";

function buildWaLinkFallback(email) {
  const base = `https://wa.me/${WA_NUMBER_FALLBACK}`;
  const e = String(email || "").trim();
  if (!e) return base;
  return `${base}?text=${encodeURIComponent("conectar " + e)}`;
}

let currentUserEmail = "";
let currentUserName = "";
let currentUserId = null;
let editingRow = null;

async function refreshWaLink() {
  const a = $("fabWhats");
  if (!a) return;

  if (!currentUserEmail) {
    a.href = buildWaLinkFallback("");
    $("fabWaLabel").textContent = "WhatsApp";
    return;
  }

  try {
    const res = await api("/api/wa_link", "GET");
    const href = (res && (res.url || res.href || res.link))
      ? (res.url || res.href || res.link)
      : "";
    a.href = href || buildWaLinkFallback(currentUserEmail);
  } catch (e) {
    a.href = buildWaLinkFallback(currentUserEmail);
  }

  $("fabWaLabel").textContent = "WhatsApp (conectar)";
}

function setLoggedUI(isLogged, email = "", name = "") {
  $("btnLogin").classList.toggle("hidden", isLogged);
  $("btnLogout").classList.toggle("hidden", !isLogged);
  $("btnEditConta").classList.toggle("hidden", !isLogged);

  $("accountEmail").textContent = isLogged ? email : "Não conectado";
  $("accountName").textContent = isLogged ? (name || firstNameFromEmail(email)) : "Conta";
  refreshWaLink();
}

async function syncSession() {
  try {
    const me = await api("/api/me", "GET");
    currentUserEmail = (me && me.email) ? String(me.email) : "";
    currentUserName = (me && me.name) ? String(me.name) : "";
    currentUserId = (me && me.user_id) ? me.user_id : null;

    setLoggedUI(!!currentUserEmail, currentUserEmail, currentUserName);
    refreshGreeting(currentUserName || currentUserEmail || "");
  } catch (e) {
    currentUserEmail = "";
    currentUserName = "";
    currentUserId = null;
    setLoggedUI(false);
    refreshGreeting("");
  }
}

async function refreshHeroSummary() {
  if (!currentUserEmail) {
    $("helloSub").textContent = "Faça login para visualizar seu resumo financeiro.";
    return;
  }

  const mes = Number($("dashMes").value);
  const ano = Number($("dashAno").value);

  try {
    const dash = await api(`/api/dashboard?mes=${mes}&ano=${ano}`, "GET");
    const score = await api("/api/score_financeiro", "GET");

    const saldo = Number(dash.saldo || 0);
    const receitas = moneyBR(dash.receitas || 0);
    const gastos = moneyBR(dash.gastos || 0);
    const saldoFmt = moneyBR(saldo);
    const scoreFmt = `${score.score || 0}/100`;

    const fraseSaldo = saldo >= 0
      ? `Você está fechando o período com saldo positivo de ${saldoFmt}.`
      : `Você está fechando o período com saldo negativo de ${saldoFmt}.`;

    $("helloSub").textContent =
      `${fraseSaldo} Receitas em ${receitas}, gastos em ${gastos} e score atual de ${scoreFmt}.`;
  } catch (e) {
    $("helloSub").textContent = "Não foi possível carregar seu resumo financeiro agora.";
  }
}

const tabEls = Array.from(document.querySelectorAll(".tab"));

function _setTabBase(name) {
  tabEls.forEach(t => t.classList.toggle("active", t.dataset.tab === name));

  ["dashboard", "lancar", "ultimos", "investimentos", "orcamentos"].forEach(n => {
    $("tab-" + n).classList.toggle("hidden", n !== name);
  });

  if (name === "dashboard") {
    refreshDashboard().catch(() => {});
    refreshIA().catch(() => {});
    refreshInsightsDashboard().catch(() => {});
    atualizarScoreFinanceiro().catch(() => {});
    refreshHeroSummary().catch(() => {});
  }

  if (name === "ultimos") carregarUltimos().catch(() => {});
  if (name === "investimentos") carregarInvestimentos().catch(() => {});
  if (name === "orcamentos") carregarOrcamentos().catch(() => {});
}

let setTab = _setTabBase;
tabEls.forEach(t => t.addEventListener("click", () => setTab(t.dataset.tab)));

$("goLancar").addEventListener("click", () => setTab("lancar"));
$("goUltimos").addEventListener("click", () => setTab("ultimos"));
$("openLogin2").addEventListener("click", () => openAuthModal("entrar"));

const overlay = $("overlay");
const segs = Array.from(document.querySelectorAll(".seg"));

function openModal() {
  overlay.classList.add("show");
  overlay.setAttribute("aria-hidden", "false");
}

function closeModal() {
  overlay.classList.remove("show");
  overlay.setAttribute("aria-hidden", "true");
  ["toastAuth1", "toastAuth2", "toastAuth3", "toastEdit", "toastConta"].forEach(id => hideToast($(id)));
}

$("closeModal").addEventListener("click", closeModal);
$("btnFechar1").addEventListener("click", closeModal);
$("btnFechar2").addEventListener("click", closeModal);
$("btnFechar3").addEventListener("click", closeModal);
$("btnFecharConta").addEventListener("click", closeModal);
overlay.addEventListener("click", (e) => {
  if (e.target === overlay) closeModal();
});

function setSeg(seg) {
  segs.forEach(s => s.classList.toggle("active", s.dataset.seg === seg));
  $("seg-entrar").classList.toggle("hidden", seg !== "entrar");
  $("seg-criar").classList.toggle("hidden", seg !== "criar");
  $("seg-reset").classList.toggle("hidden", seg !== "reset");
  $("seg-conta").classList.toggle("hidden", seg !== "conta");

  $("modalIcon").textContent = seg === "conta" ? "👤" : "🔒";
  $("modalTitle").textContent =
    seg === "entrar" ? "Login" :
    seg === "criar" ? "Criar conta" :
    seg === "reset" ? "Resetar senha" :
    "Minha conta";

  $("modalSub").textContent =
    seg === "entrar" ? "Entre com seu e-mail e senha." :
    seg === "criar" ? "Crie sua conta para acessar seus lançamentos." :
    seg === "reset" ? "Defina uma nova senha para sua conta." :
    "Edite o nome que aparece na saudação.";

  $("authBlock").classList.remove("hidden");
  $("editBlock").classList.add("hidden");
  openModal();
}

segs.forEach(s => s.addEventListener("click", () => setSeg(s.dataset.seg)));

function openAuthModal(seg = "entrar") {
  setSeg(seg);
}

function openContaModal() {
  $("segContaTab").classList.remove("hidden");
  $("contaNome").value = currentUserName || "";
  $("contaEmail").value = currentUserEmail || "";
  setSeg("conta");
}

$("btnEntrar").addEventListener("click", async () => {
  const t = $("toastAuth1");
  hideToast(t);
  try {
    const email = $("loginEmail").value.trim().toLowerCase();
    const senha = $("loginSenha").value;
    const res = await api("/api/login", "POST", { email, senha });

    currentUserEmail = (res && res.email) ? res.email : email;
    currentUserName = (res && res.name) ? res.name : "";
    await syncSession();

    showToast(t, "ok", "Login realizado", "Você já pode usar o app.");
    setTimeout(async () => {
      closeModal();
      await refreshDashboard().catch(() => {});
      await refreshIA().catch(() => {});
      await refreshInsightsDashboard().catch(() => {});
      await atualizarScoreFinanceiro().catch(() => {});
      await carregarUltimos().catch(() => {});
      await carregarInvestimentos().catch(() => {});
      await carregarOrcamentos().catch(() => {});
      await refreshHeroSummary().catch(() => {});
    }, 600);
  } catch (e) {
    showToast(t, "err", "Falha no login", e.message);
  }
});

$("btnCriarConta").addEventListener("click", async () => {
  const t = $("toastAuth2");
  hideToast(t);
  try {
    const nome_apelido = $("regApelido").value.trim();
    const nome_completo = $("regNomeCompleto").value.trim();
    const telefone = $("regTelefone").value.trim();
    const email = $("regEmail").value.trim().toLowerCase();
    const senha = $("regSenha").value;
    const confirmar_senha = $("regConf").value;

    const res = await api("/api/register", "POST", {
      nome_apelido,
      nome_completo,
      telefone,
      email,
      senha,
      confirmar_senha
    });

    currentUserEmail = (res && res.email) ? res.email : email;
    currentUserName = (res && res.name) ? res.name : "";
    await syncSession();

    showToast(t, "ok", "Conta criada", "Cadastro feito! Você já está logado.");
    setTimeout(async () => {
      closeModal();
      await refreshDashboard().catch(() => {});
      await refreshIA().catch(() => {});
      await refreshInsightsDashboard().catch(() => {});
      await atualizarScoreFinanceiro().catch(() => {});
      await carregarUltimos().catch(() => {});
      await carregarInvestimentos().catch(() => {});
      await carregarOrcamentos().catch(() => {});
      await refreshHeroSummary().catch(() => {});
    }, 600);
  } catch (e) {
    showToast(t, "err", "Erro ao cadastrar", e.message);
  }
});

$("btnResetar").addEventListener("click", async () => {
  const t = $("toastAuth3");
  hideToast(t);
  try {
    const email = $("rstEmail").value.trim().toLowerCase();
    const nova_senha = $("rstSenha").value;
    const confirmar = $("rstConf").value;
    await api("/api/reset_password", "POST", { email, nova_senha, confirmar });
    showToast(t, "ok", "Senha alterada", "Agora você já pode entrar com a nova senha.");
  } catch (e) {
    showToast(t, "err", "Falha no reset", e.message);
  }
});

$("btnSalvarConta").addEventListener("click", async () => {
  const t = $("toastConta");
  hideToast(t);

  try {
    const nome = $("contaNome").value.trim();
    if (!nome) throw new Error("Informe um nome para exibição.");

    const res = await api("/api/account", "POST", { name: nome });

    currentUserName = (res && res.name) ? res.name : nome;
    await syncSession();
    refreshGreeting(currentUserName || currentUserEmail || "");
    await refreshHeroSummary().catch(() => {});

    showToast(t, "ok", "Conta atualizada", "Seu nome de exibição foi salvo.");
  } catch (e) {
    showToast(t, "err", "Erro ao salvar conta", e.message);
  }
});

$("btnLogout").addEventListener("click", async () => {
  try { await api("/api/logout", "POST"); } catch (e) {}

  currentUserEmail = "";
  currentUserName = "";
  currentUserId = null;

  setLoggedUI(false);
  refreshGreeting("");
  $("helloSub").textContent = "Faça login para visualizar seu resumo financeiro.";

  refreshDashboard().catch(() => {});
  refreshIA().catch(() => {});
  refreshInsightsDashboard().catch(() => {});
  atualizarScoreFinanceiro().catch(() => {});
  carregarUltimos().catch(() => {});
  carregarInvestimentos().catch(() => {});
  carregarOrcamentos().catch(() => {});
  toggleAccountMenu(false);
});

const btnAccount = $("btnAccount");
const accountDropdown = $("accountDropdown");

function toggleAccountMenu(force = null) {
  const open = force === null ? !accountDropdown.classList.contains("show") : !!force;
  accountDropdown.classList.toggle("show", open);
}

btnAccount.addEventListener("click", (e) => {
  e.stopPropagation();
  toggleAccountMenu();
});

$("btnLogin").addEventListener("click", () => {
  toggleAccountMenu(false);
  openAuthModal("entrar");
});

$("btnEditConta").addEventListener("click", () => {
  toggleAccountMenu(false);
  openContaModal();
});

document.addEventListener("click", (e) => {
  if (!accountDropdown.contains(e.target) && e.target !== btnAccount) {
    toggleAccountMenu(false);
  }
});

let chartRG = null;
let chartSaldo = null;
let chartDaily = null;
let chartPatrimonio = null;
let chartCategorias = null;

function ensureCharts() {
  if (!window.Chart) return;

  if ($("chartRG") && !chartRG) {
    chartRG = new Chart($("chartRG"), {
      type: "doughnut",
      data: {
        labels: ["Receitas", "Gastos"],
        datasets: [{
          data: [0, 0],
          backgroundColor: ["rgba(46,229,157,.85)", "rgba(255,75,110,.85)"],
          borderColor: ["rgba(46,229,157,.20)", "rgba(255,75,110,.20)"],
          borderWidth: 1,
          hoverOffset: 6
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { labels: { color: "rgba(234,240,255,.85)" } },
          tooltip: { callbacks: { label: (ctx) => `${ctx.label}: ${moneyBR(ctx.raw || 0)}` } }
        },
        cutout: "65%"
      }
    });
  }

  if ($("chartSaldo") && !chartSaldo) {
    chartSaldo = new Chart($("chartSaldo"), {
      type: "bar",
      data: {
        labels: ["Saldo"],
        datasets: [{
          label: "Saldo do período",
          data: [0],
          backgroundColor: ["rgba(75,140,255,.75)"],
          borderColor: ["rgba(75,140,255,.25)"],
          borderWidth: 1
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: (ctx) => moneyBR(ctx.raw || 0) } }
        },
        scales: {
          x: { ticks: { color: "rgba(234,240,255,.75)" }, grid: { color: "rgba(255,255,255,.06)" } },
          y: { ticks: { color: "rgba(234,240,255,.75)" }, grid: { color: "rgba(255,255,255,.06)" } }
        }
      }
    });
  }

  if ($("chartDaily") && !chartDaily) {
    chartDaily = new Chart($("chartDaily"), {
      type: "line",
      data: {
        labels: [],
        datasets: [
          {
            label: "Receitas (dia)",
            data: [],
            borderColor: "rgba(46,229,157,.9)",
            backgroundColor: "rgba(46,229,157,.12)",
            tension: 0.25,
            fill: true,
            pointRadius: 2
          },
          {
            label: "Gastos (dia)",
            data: [],
            borderColor: "rgba(255,75,110,.9)",
            backgroundColor: "rgba(255,75,110,.10)",
            tension: 0.25,
            fill: true,
            pointRadius: 2
          },
          {
            label: "Saldo acumulado",
            data: [],
            borderColor: "rgba(75,140,255,.95)",
            backgroundColor: "rgba(75,140,255,.12)",
            tension: 0.25,
            fill: false,
            pointRadius: 1
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { labels: { color: "rgba(234,240,255,.85)" } },
          tooltip: { callbacks: { label: (ctx) => `${ctx.dataset.label}: ${moneyBR(ctx.raw || 0)}` } }
        },
        scales: {
          x: { ticks: { color: "rgba(234,240,255,.75)" }, grid: { color: "rgba(255,255,255,.06)" } },
          y: { ticks: { color: "rgba(234,240,255,.75)" }, grid: { color: "rgba(255,255,255,.06)" } }
        }
      }
    });
  }

  if ($("chartPatrimonio") && !chartPatrimonio) {
    chartPatrimonio = new Chart($("chartPatrimonio"), {
      type: "line",
      data: {
        labels: [],
        datasets: [{
          label: "Patrimônio",
          data: [],
          borderColor: "rgba(255,184,77,.95)",
          backgroundColor: "rgba(255,184,77,.12)",
          tension: 0.25,
          fill: true,
          pointRadius: 3
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { labels: { color: "rgba(234,240,255,.85)" } },
          tooltip: { callbacks: { label: (ctx) => `${ctx.dataset.label}: ${moneyBR(ctx.raw || 0)}` } }
        },
        scales: {
          x: { ticks: { color: "rgba(234,240,255,.75)" }, grid: { color: "rgba(255,255,255,.06)" } },
          y: { ticks: { color: "rgba(234,240,255,.75)" }, grid: { color: "rgba(255,255,255,.06)" } }
        }
      }
    });
  }

  if ($("chartCategorias") && !chartCategorias) {
    chartCategorias = new Chart($("chartCategorias"), {
      type: "doughnut",
      data: {
        labels: [],
        datasets: [{
          data: [],
          backgroundColor: [
            "rgba(75,140,255,.85)",
            "rgba(46,229,157,.85)",
            "rgba(255,184,77,.85)",
            "rgba(255,75,110,.85)",
            "rgba(140,120,255,.85)",
            "rgba(120,220,255,.85)"
          ],
          borderColor: "rgba(255,255,255,.08)",
          borderWidth: 1,
          hoverOffset: 6
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { labels: { color: "rgba(234,240,255,.85)" } },
          tooltip: { callbacks: { label: (ctx) => `${ctx.label}: ${moneyBR(ctx.raw || 0)}` } }
        },
        cutout: "60%"
      }
    });
  }
}

function updateChartsTotals(receitas, gastos, saldo) {
  ensureCharts();

  if (chartRG) {
    chartRG.data.datasets[0].data = [Number(receitas || 0), Number(gastos || 0)];
    chartRG.update();
  }

  if (chartSaldo) {
    const s = Number(saldo || 0);
    chartSaldo.data.datasets[0].data = [s];
    chartSaldo.data.datasets[0].backgroundColor = [s >= 0 ? "rgba(46,229,157,.75)" : "rgba(255,75,110,.75)"];
    chartSaldo.data.datasets[0].borderColor = [s >= 0 ? "rgba(46,229,157,.25)" : "rgba(255,75,110,.25)"];
    chartSaldo.update();
  }
}

function daysInMonth(year, month1to12) {
  return new Date(year, month1to12, 0).getDate();
}

function parseISODate(s) {
  const parts = String(s || "").split("-");
  if (parts.length !== 3) return null;
  const y = Number(parts[0]), m = Number(parts[1]), d = Number(parts[2]);
  if (!y || !m || !d) return null;
  return { y, m, d };
}

async function updateDailyChart(mes, ano) {
  ensureCharts();
  if (!chartDaily) return;

  const dim = daysInMonth(ano, mes);
  const labels = Array.from({ length: dim }, (_, i) => String(i + 1));
  const receitasDia = Array.from({ length: dim }, () => 0);
  const gastosDia = Array.from({ length: dim }, () => 0);

  try {
    const res = await api("/api/lancamentos?limit=200", "GET");
    const items = res.items || [];

    for (const it of items) {
      const dt = parseISODate(it.data);
      if (!dt) continue;
      if (dt.y !== ano || dt.m !== mes) continue;
      const idx = dt.d - 1;
      if (idx < 0 || idx >= dim) continue;

      const tipo = String(it.tipo || "").toUpperCase();
      const val = Number(it.valor || 0);

      if (tipo === "RECEITA") receitasDia[idx] += val;
      if (tipo === "GASTO") gastosDia[idx] += val;
    }
  } catch (e) {}

  const saldoAcum = [];
  let acc = 0;
  for (let i = 0; i < dim; i++) {
    acc += (receitasDia[i] - gastosDia[i]);
    saldoAcum.push(acc);
  }

  chartDaily.data.labels = labels;
  chartDaily.data.datasets[0].data = receitasDia;
  chartDaily.data.datasets[1].data = gastosDia;
  chartDaily.data.datasets[2].data = saldoAcum;
  chartDaily.update();
}

async function updatePatrimonioChart() {
  ensureCharts();
  if (!chartPatrimonio) return;
  try {
    const res = await api("/api/patrimonio?months=6", "GET");
    chartPatrimonio.data.labels = res.labels || [];
    chartPatrimonio.data.datasets[0].data = res.values || [];
    chartPatrimonio.update();
  } catch (e) {
    chartPatrimonio.data.labels = [];
    chartPatrimonio.data.datasets[0].data = [];
    chartPatrimonio.update();
  }
}

async function refreshDashboard() {
  const toast = $("toastDash");
  hideToast(toast);

  const mes = Number($("dashMes").value);
  const ano = Number($("dashAno").value);

  try {
    const res = await api(`/api/dashboard?mes=${mes}&ano=${ano}`, "GET");

    $("valReceitas").textContent = moneyBR(res.receitas);
    $("valGastos").textContent = moneyBR(res.gastos);
    $("valSaldo").textContent = moneyBR(res.saldo);

    const saldoEl = $("valSaldo");
    saldoEl.classList.remove("positive", "negative");
    saldoEl.classList.add(Number(res.saldo) >= 0 ? "positive" : "negative");

    updateChartsTotals(res.receitas, res.gastos, res.saldo);
    await updateDailyChart(mes, ano);
  } catch (e) {
    $("valReceitas").textContent = "R$ 0,00";
    $("valGastos").textContent = "R$ 0,00";
    $("valSaldo").textContent = "R$ 0,00";
    updateChartsTotals(0, 0, 0);
    await updateDailyChart(mes, ano);
    showToast(toast, "err", "Faça login para ver o painel", e.message);
  }
}

async function refreshIA() {
  const toast = $("toastIA");
  hideToast(toast);

  try {
    const proj = await api("/api/projecao", "GET");

    $("valSaldoPrevisto").textContent = moneyBR(proj.saldo_previsto);
    $("valGastoMedioDia").textContent = moneyBR(proj.gasto_medio_diario);
    $("valEstimativaRestante").textContent = moneyBR(proj.estimativa_gastos_restantes);

    const saldoPrev = $("valSaldoPrevisto");
    saldoPrev.classList.remove("positive", "negative", "warn");
    if (Number(proj.saldo_previsto) < 0) saldoPrev.classList.add("negative");
    else saldoPrev.classList.add("positive");

    $("txtProjecaoResumo").textContent =
      proj.alerta_negativo
        ? `Atenção: projeção negativa em ${proj.dias_restantes} dia(s).`
        : `Projeção saudável para os próximos ${proj.dias_restantes} dia(s).`;

    const alerts = await api("/api/alertas", "GET");
    renderAlertas(alerts.items || []);

    await updatePatrimonioChart();
  } catch (e) {
    $("valSaldoPrevisto").textContent = "R$ 0,00";
    $("valGastoMedioDia").textContent = "R$ 0,00";
    $("valEstimativaRestante").textContent = "R$ 0,00";
    $("txtProjecaoResumo").textContent = "Faça login para usar a projeção.";
    renderAlertas([]);
    await updatePatrimonioChart();
    showToast(toast, "err", "IA indisponível", e.message);
  }
}

function renderAlertas(items) {
  const box = $("listaAlertas");
  if (!items || !items.length) {
    box.innerHTML = `<div class="muted">Nenhum alerta importante no momento.</div>`;
    return;
  }

  box.innerHTML = items.map(a => `
    <div class="alert-item ${String(a.nivel || "").toLowerCase()}">
      <div class="alert-title">${escapeHtml(a.titulo || "Alerta")}</div>
      <div class="alert-msg">${escapeHtml(a.mensagem || "")}</div>
    </div>
  `).join("");
}

function renderOrcamentoAlertas(items) {
  const box = $("orcAlertas");
  if (!items || !items.length) {
    box.innerHTML = `<div class="muted">Nenhum alerta de orçamento por enquanto.</div>`;
    return;
  }

  box.innerHTML = items.map(a => `
    <div class="alert-item ${String(a.status || "").toLowerCase() === "excedido" ? "high" : "medium"}">
      <div class="alert-title">${escapeHtml(a.categoria || "Categoria")}</div>
      <div class="alert-msg">
        Meta: ${moneyBR(a.meta)} • Gasto: ${moneyBR(a.gasto)} • Restante: ${moneyBR(a.restante)}<br>
        Uso: ${Number(a.percentual || 0).toFixed(0)}%
      </div>
    </div>
  `).join("");
}

function updateOrcResumo(items) {
  let meta = 0;
  let gasto = 0;

  for (const item of items || []) {
    if (String(item.categoria || "").toUpperCase() === "TOTAL") {
      meta = Number(item.meta || 0);
      gasto = Number(item.gasto || 0);
      break;
    }
  }

  if (!meta && items && items.length) {
    meta = items.reduce((acc, i) => acc + Number(i.meta || 0), 0);
    gasto = items.reduce((acc, i) => acc + (String(i.categoria || "").toUpperCase() === "TOTAL" ? 0 : Number(i.gasto || 0)), 0);
  }

  $("orcResumoMeta").textContent = moneyBR(meta);
  $("orcResumoGasto").textContent = moneyBR(gasto);
  $("orcResumoRestante").textContent = moneyBR(meta - gasto);

  $("orcResumoRestante").classList.remove("positive", "negative");
  $("orcResumoRestante").classList.add((meta - gasto) >= 0 ? "positive" : "negative");
}

async function carregarOrcamentos() {
  const toast = $("toastOrc");
  const list = $("listaOrcamentos");
  hideToast(toast);
  list.innerHTML = "";

  if (!currentUserEmail) {
    list.innerHTML = `<div class="muted">Faça login para gerenciar seus orçamentos.</div>`;
    $("orcAlertas").innerHTML = `<div class="muted">Nenhum alerta de orçamento por enquanto.</div>`;
    updateOrcResumo([]);
    return;
  }

  try {
    const mes = Number($("orcMes").value);
    const ano = Number($("orcAno").value);
    const res = await api(`/api/orcamentos?mes=${mes}&ano=${ano}`, "GET");
    const items = res.items || [];

    if (items.length === 0) {
      list.innerHTML = `<div class="muted">Nenhum orçamento cadastrado para este período.</div>`;
      $("orcAlertas").innerHTML = `<div class="muted">Nenhum alerta de orçamento por enquanto.</div>`;
      updateOrcResumo([]);
      return;
    }

    updateOrcResumo(items);
    renderOrcamentoAlertas(items.filter(i => i.status === "atencao" || i.status === "excedido"));

    list.innerHTML = items.map(it => {
      const percent = Number(it.percentual || 0);
      const barColor =
        it.status === "excedido" ? "linear-gradient(90deg, rgba(255,75,110,.95), rgba(255,110,140,.95))" :
        it.status === "atencao" ? "linear-gradient(90deg, rgba(255,184,77,.95), rgba(255,210,120,.95))" :
        "linear-gradient(90deg, rgba(46,229,157,.95), rgba(108,241,195,.95))";

      return `
        <div class="miniCard">
          <div class="row" style="align-items:flex-start;gap:12px">
            <div style="flex:1">
              <div style="font-weight:900">${escapeHtml(it.categoria || "TOTAL")}</div>
              <div class="muted" style="margin-top:4px;font-size:13px">
                Meta: ${moneyBR(it.meta)} • Gasto: ${moneyBR(it.gasto)} • Restante: ${moneyBR(it.restante)}
              </div>
              <div class="orc-progress">
                <div style="width:${Math.min(percent, 100)}%;background:${barColor}"></div>
              </div>
              <div class="hint" style="margin-top:6px">
                Uso: ${percent.toFixed(0)}% • 
                <span class="${
                  it.status === "excedido" ? "orc-status-excedido" :
                  it.status === "atencao" ? "orc-status-atencao" :
                  "orc-status-ok"
                }">${escapeHtml(it.status)}</span>
              </div>
            </div>
            <div>
              <button class="btn danger small" onclick="apagarOrcamento(${it.id})">Apagar</button>
            </div>
          </div>
        </div>
      `;
    }).join("");
  } catch (e) {
    showToast(toast, "err", "Erro ao carregar orçamentos", e.message);
    list.innerHTML = `<div class="muted">Não foi possível carregar os orçamentos.</div>`;
  }
}

window.apagarOrcamento = async (id) => {
  const toast = $("toastOrc");
  hideToast(toast);

  if (!confirm("Quer mesmo apagar este orçamento?")) return;

  try {
    await api(`/api/orcamentos/${id}`, "DELETE");
    showToast(toast, "ok", "Apagado!", "Orçamento removido.");
    await carregarOrcamentos();
  } catch (e) {
    showToast(toast, "err", "Não foi possível apagar", e.message);
  }
};

$("btnSalvarOrc").addEventListener("click", async () => {
  const toast = $("toastOrc");
  hideToast(toast);

  try {
    const payload = {
      mes: Number($("orcMes").value),
      ano: Number($("orcAno").value),
      categoria: $("orcCategoria").value.trim() || "TOTAL",
      valor_meta: $("orcValorMeta").value.trim()
    };

    await api("/api/orcamentos", "POST", payload);
    showToast(toast, "ok", "Salvo!", "Orçamento registrado com sucesso.");
    $("orcCategoria").value = "";
    $("orcValorMeta").value = "";
    await carregarOrcamentos();
  } catch (e) {
    showToast(toast, "err", "Erro ao salvar orçamento", e.message);
  }
});

$("btnLimparOrc").addEventListener("click", () => {
  $("orcCategoria").value = "";
  $("orcValorMeta").value = "";
  hideToast($("toastOrc"));
});

$("btnRecarregarOrc").addEventListener("click", () => carregarOrcamentos());
$("orcMes").addEventListener("change", () => carregarOrcamentos().catch(() => {}));
$("orcAno").addEventListener("change", () => carregarOrcamentos().catch(() => {}));

async function refreshInsightsDashboard() {
  const mes = Number($("dashMes").value);
  const ano = Number($("dashAno").value);

  try {
    const res = await api(`/api/insights_dashboard?mes=${mes}&ano=${ano}`, "GET");

    $("valScore").textContent = `${res.score || 0}/100`;
    $("txtInsightAuto").textContent = res.insight || "Sem insight disponível.";

    const scoreEl = $("valScore");
    scoreEl.classList.remove("positive", "negative", "warn");

    const scoreNum = Number(res.score || 0);
    if (scoreNum >= 80) scoreEl.classList.add("positive");
    else if (scoreNum >= 60) scoreEl.classList.add("warn");
    else scoreEl.classList.add("negative");

    if (res.status === "saudavel") $("txtScoreHint").textContent = "Saúde financeira boa.";
    else if (res.status === "atencao") $("txtScoreHint").textContent = "Atenção moderada no período.";
    else if (res.status === "critico") $("txtScoreHint").textContent = "Seu período exige mais atenção.";
    else $("txtScoreHint").textContent = "Análise concluída.";

    ensureCharts();
    if (chartCategorias) {
      chartCategorias.data.labels = res.categorias || [];
      chartCategorias.data.datasets[0].data = res.valores || [];
      chartCategorias.update();
    }
  } catch (e) {
    $("valScore").textContent = "0/100";
    $("txtScoreHint").textContent = "Faça login para visualizar.";
    $("txtInsightAuto").textContent = "Sem dados para análise.";

    if (chartCategorias) {
      chartCategorias.data.labels = [];
      chartCategorias.data.datasets[0].data = [];
      chartCategorias.update();
    }
  }
}

$("btnAtualizarDash").addEventListener("click", async () => {
  await refreshDashboard();
  await refreshInsightsDashboard();
  await atualizarScoreFinanceiro();
  await refreshHeroSummary();
});

$("btnAtualizarIA").addEventListener("click", async () => {
  await refreshIA();
  await refreshInsightsDashboard();
  await atualizarScoreFinanceiro();
  await refreshHeroSummary();
});

$("btnVerAlertas").addEventListener("click", async () => {
  await refreshIA();
  await refreshInsightsDashboard();
  await atualizarScoreFinanceiro();
  await refreshHeroSummary();
});

$("dashMes").addEventListener("change", () => {
  refreshDashboard().catch(() => {});
  refreshInsightsDashboard().catch(() => {});
  atualizarScoreFinanceiro().catch(() => {});
  refreshHeroSummary().catch(() => {});
});

$("dashAno").addEventListener("change", () => {
  refreshDashboard().catch(() => {});
  refreshInsightsDashboard().catch(() => {});
  atualizarScoreFinanceiro().catch(() => {});
  refreshHeroSummary().catch(() => {});
});

$("btnSalvarLanc").addEventListener("click", async () => {
  const toast = $("toastLanc");
  hideToast(toast);

  try {
    const payload = {
      tipo: $("lanTipo").value,
      data: brToISO($("lanData").value),
      categoria: $("lanCategoria").value.trim(),
      descricao: $("lanDescricao").value.trim(),
      valor: $("lanValor").value.trim(),
    };

    await api("/api/lancamentos", "POST", payload);
    showToast(toast, "ok", "Salvo!", "Lançamento registrado com sucesso.");

    $("lanCategoria").value = "";
    $("lanDescricao").value = "";
    $("lanValor").value = "";

    refreshDashboard().catch(() => {});
    refreshIA().catch(() => {});
    refreshInsightsDashboard().catch(() => {});
    atualizarScoreFinanceiro().catch(() => {});
    carregarUltimos().catch(() => {});
    carregarOrcamentos().catch(() => {});
    refreshHeroSummary().catch(() => {});
  } catch (e) {
    showToast(toast, "err", "Erro ao salvar", e.message);
  }
});

$("btnLimparLanc").addEventListener("click", () => {
  $("lanCategoria").value = "";
  $("lanDescricao").value = "";
  $("lanValor").value = "";
  hideToast($("toastLanc"));
});

$("btnRecarregarUltimos").addEventListener("click", () => carregarUltimos());

function normalizeValorToMoneyBR(raw) {
  const n = Number(String(raw ?? "").trim().replace(/\s/g, "").replace(",", "."));
  if (!isNaN(n)) {
    return n.toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  return String(raw ?? "");
}

async function carregarUltimos() {
  const toast = $("toastUltimos");
  const list = $("listaUltimos");
  hideToast(toast);
  list.innerHTML = "";

  try {
    const res = await api("/api/lancamentos?limit=30", "GET");
    const items = res.items || [];

    if (items.length === 0) {
      list.innerHTML = `<div class="muted">Nenhum lançamento ainda.</div>`;
      return;
    }

    list.innerHTML = items.map(it => {
      const v = moneyBR(Number(it.valor || 0));
      const desc = escapeHtml(it.descricao || "");
      const cat = escapeHtml(it.categoria || "Sem categoria");

      return `
        <div class="miniCard">
          <div class="row" style="gap:12px;align-items:flex-start">
            <div style="min-width:92px" class="muted"><b>${isoToBR(it.data)}</b></div>
            <div style="flex:1">
              <div style="font-weight:900">${escapeHtml(it.tipo)} • ${cat}</div>
              <div class="muted" style="margin-top:4px;font-size:13px">${desc}</div>
            </div>
            <div style="text-align:right;min-width:130px">
              <div style="font-weight:900">${v}</div>
              <div class="row" style="justify-content:flex-end;margin-top:8px">
                <button class="btn small" onclick='abrirEdicao(${JSON.stringify(it).replace(/'/g, "&#39;")})'>Editar</button>
                <button class="btn danger small" onclick="apagarLancamento(${it.row})">Apagar</button>
              </div>
            </div>
          </div>
        </div>
      `;
    }).join("");
  } catch (e) {
    showToast(toast, "err", "Erro ao carregar", e.message);
  }
}

async function apagarLancamento(row) {
  const toast = $("toastUltimos");
  hideToast(toast);

  if (!confirm("Quer mesmo apagar este lançamento?")) return;

  try {
    await api(`/api/lancamentos/${row}`, "DELETE");
    showToast(toast, "ok", "Apagado!", "Lançamento removido.");

    await carregarUltimos();
    await refreshWaLink();
    await refreshDashboard();
    await refreshIA();
    await refreshInsightsDashboard();
    await atualizarScoreFinanceiro();
    await carregarOrcamentos();
    await refreshHeroSummary();
  } catch (e) {
    showToast(toast, "err", "Não foi possível apagar", e.message);
  }
}

function openEditModal() {
  $("authBlock").classList.add("hidden");
  $("editBlock").classList.remove("hidden");
  $("modalIcon").textContent = "✏️";
  $("modalTitle").textContent = "Editar lançamento";
  $("modalSub").textContent = "Edite e salve.";
  openModal();
}

window.abrirEdicao = (it) => {
  if (!currentUserEmail) return openAuthModal("entrar");

  editingRow = it.row;
  $("edtTipo").value = (it.tipo || "GASTO").toUpperCase();
  $("edtData").value = isoToBR(it.data) || isoToBR(new Date().toISOString().slice(0, 10));
  $("edtCategoria").value = it.categoria || "";
  $("edtDescricao").value = it.descricao || "";
  $("edtValor").value = normalizeValorToMoneyBR(it.valor);

  hideToast($("toastEdit"));
  openEditModal();
};

window.apagarLancamento = apagarLancamento;

$("btnCancelarEdicao").addEventListener("click", () => {
  editingRow = null;
  closeModal();
});

$("btnSalvarEdicao").addEventListener("click", async () => {
  const toast = $("toastEdit");
  hideToast(toast);

  if (!editingRow) {
    showToast(toast, "err", "Nada para editar", "Selecione um lançamento para editar.");
    return;
  }

  try {
    const payload = {
      tipo: $("edtTipo").value,
      data: brToISO($("edtData").value),
      categoria: $("edtCategoria").value.trim(),
      descricao: $("edtDescricao").value.trim(),
      valor: $("edtValor").value.trim(),
    };

    await api(`/api/lancamentos/${editingRow}`, "PUT", payload);
    showToast(toast, "ok", "Atualizado!", "Lançamento editado com sucesso.");

    await carregarUltimos();
    await refreshWaLink();
    await refreshDashboard();
    await refreshIA();
    await refreshInsightsDashboard();
    await atualizarScoreFinanceiro();
    await carregarOrcamentos();
    await refreshHeroSummary();

    setTimeout(() => { closeModal(); }, 450);
  } catch (e) {
    showToast(toast, "err", "Erro ao editar", e.message);
  }
});

$("btnRecarregarInv").addEventListener("click", () => carregarInvestimentos());

async function carregarInvestimentos() {
  const toast = $("toastInv");
  const list = $("listaInv");
  hideToast(toast);
  list.innerHTML = "";

  if (!currentUserEmail) {
    showToast(toast, "err", "Faça login", "Entre para ver e registrar investimentos.");
    return;
  }

  try {
    const res = await api("/api/investimentos?limit=50", "GET");
    const items = res.items || [];

    if (items.length === 0) {
      list.innerHTML = `<div class="muted">Nenhum investimento ainda.</div>`;
      return;
    }

    list.innerHTML = items.map(it => {
      const v = moneyBR(Number(String(it.valor).replace(",", ".")));
      const desc = escapeHtml(it.descricao || "");
      const ativo = escapeHtml(it.ativo || "");
      const tipo = escapeHtml((it.tipo || "APORTE").toUpperCase());

      return `
        <div class="miniCard">
          <div class="row" style="gap:12px;align-items:flex-start">
            <div style="min-width:92px" class="muted"><b>${isoToBR(it.data)}</b></div>
            <div style="flex:1">
              <div style="font-weight:900">${tipo} • ${ativo}</div>
              <div class="muted" style="margin-top:4px;font-size:13px">${desc}</div>
            </div>
            <div style="text-align:right;min-width:130px">
              <div style="font-weight:900">${v}</div>
              <div class="row" style="justify-content:flex-end;margin-top:8px">
                <button class="btn danger small" onclick="apagarInvestimento(${it.id})">Apagar</button>
              </div>
            </div>
          </div>
        </div>
      `;
    }).join("");
  } catch (e) {
    showToast(toast, "err", "Erro ao carregar investimentos", e.message);
  }
}

window.apagarInvestimento = async (id) => {
  const toast = $("toastInv");
  hideToast(toast);

  if (!confirm("Quer mesmo apagar este investimento?")) return;

  try {
    await api(`/api/investimentos/${id}`, "DELETE");
    showToast(toast, "ok", "Apagado!", "Investimento removido.");

    await carregarInvestimentos();
    await refreshIA();
    await refreshInsightsDashboard();
    await atualizarScoreFinanceiro();
    await refreshHeroSummary();
  } catch (e) {
    showToast(toast, "err", "Não foi possível apagar", e.message);
  }
};

$("btnSalvarInv").addEventListener("click", async () => {
  const toast = $("toastInv");
  hideToast(toast);

  try {
    const payload = {
      data: brToISO($("invData").value),
      tipo: $("invTipo").value,
      ativo: $("invAtivo").value.trim(),
      valor: $("invValor").value.trim(),
      descricao: $("invDescricao").value.trim(),
    };

    await api("/api/investimentos", "POST", payload);
    showToast(toast, "ok", "Salvo!", "Investimento registrado.");

    $("invAtivo").value = "";
    $("invValor").value = "";
    $("invDescricao").value = "";

    await carregarInvestimentos();
    await refreshIA();
    await refreshInsightsDashboard();
    await atualizarScoreFinanceiro();
    await refreshHeroSummary();
  } catch (e) {
    showToast(toast, "err", "Erro ao salvar", e.message);
  }
});

$("btnLimparInv").addEventListener("click", () => {
  $("invAtivo").value = "";
  $("invValor").value = "";
  $("invDescricao").value = "";
  hideToast($("toastInv"));
});

const fabWrap = $("fabWrap");
const fabMain = $("fabMain");
const fabBackdrop = $("fabBackdrop");
const fabNovo = $("fabNovo");
const fabDash = $("fabDash");

function fabClose() {
  if (!fabWrap) return;
  fabWrap.classList.remove("open");
  fabMain.setAttribute("aria-expanded", "false");
  fabWrap.querySelector(".fab-actions").setAttribute("aria-hidden", "true");
  $("fabMainIco").textContent = "☰";
}

function fabToggle() {
  if (!fabWrap) return;
  const open = !fabWrap.classList.contains("open");
  fabWrap.classList.toggle("open", open);
  fabMain.setAttribute("aria-expanded", open ? "true" : "false");
  fabWrap.querySelector(".fab-actions").setAttribute("aria-hidden", open ? "false" : "true");
  $("fabMainIco").textContent = open ? "✕" : "☰";
}

fabMain.addEventListener("click", fabToggle);
fabBackdrop.addEventListener("click", fabClose);
fabNovo.addEventListener("click", (e) => {
  e.preventDefault();
  fabClose();
  setTab("lancar");
});
fabDash.addEventListener("click", (e) => {
  e.preventDefault();
  fabClose();
  setTab("dashboard");
});
tabEls.forEach(t => t.addEventListener("click", fabClose));

function fillMonthSelect(selectId) {
  const sel = $(selectId);
  const now = new Date();
  const meses = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho", "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"];
  sel.innerHTML = meses.map((m, i) => `<option value="${i + 1}">${m}</option>`).join("");
  sel.value = String(now.getMonth() + 1);
}

function initMesAno() {
  const now = new Date();
  fillMonthSelect("dashMes");
  fillMonthSelect("orcMes");
  $("dashAno").value = String(now.getFullYear());
  $("orcAno").value = String(now.getFullYear());
  $("lanData").value = isoToBR(now.toISOString().slice(0, 10));
  $("invData").value = isoToBR(now.toISOString().slice(0, 10));
}

initMesAno();
ensureCharts();

syncSession().finally(async () => {
  refreshWaLink();
  await refreshDashboard().catch(() => {});
  await refreshIA().catch(() => {});
  await refreshInsightsDashboard().catch(() => {});
  await atualizarScoreFinanceiro().catch(() => {});
  await carregarOrcamentos().catch(() => {});
  await refreshHeroSummary().catch(() => {});
});

if ("serviceWorker" in navigator) {
  window.addEventListener("load", async () => {
    try {
      const reg = await navigator.serviceWorker.register("/static/sw.js");
      console.log("Service Worker registrado:", reg.scope);
    } catch (err) {
      console.error("Erro ao registrar Service Worker:", err);
    }
  });
}
