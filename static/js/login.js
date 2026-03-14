const $ = (id) => document.getElementById(id);

async function api(path, method = "GET", body = null) {
  const opt = {
    method,
    headers: { "Content-Type": "application/json" },
    credentials: "include",
  };

  if (body) {
    opt.body = JSON.stringify(body);
  }

  const res = await fetch(path, opt);

  let data = null;
  try {
    data = await res.json();
  } catch (e) {}

  if (!res.ok) {
    const msg =
      data && (data.error || data.message)
        ? (data.error || data.message)
        : `Erro ${res.status}`;
    throw new Error(msg);
  }

  return data;
}

function showToast(el, type, title, desc = "") {
  if (!el) return;
  el.className =
    "toast show " + (type === "ok" ? "ok" : type === "warn" ? "warn" : "err");
  el.innerHTML =
    `<div class="t">${title}</div>` +
    (desc ? `<div class="d">${desc}</div>` : "");
}

function hideToast(el) {
  if (!el) return;
  el.className = "toast";
  el.innerHTML = "";
}

function hideAllToasts() {
  ["toastLoginPage", "toastRegisterPage", "toastResetPage"].forEach((id) => {
    hideToast($(id));
  });
}

function setAuthTab(tabName) {
  document.querySelectorAll("[data-auth-tab]").forEach((el) => {
    el.classList.toggle("active", el.dataset.authTab === tabName);
  });

  ["entrar", "criar", "reset"].forEach((name) => {
    const panel = $(`login-tab-${name}`);
    if (!panel) return;
    panel.classList.toggle("hidden", name !== tabName);
  });

  hideAllToasts();
}

async function checkExistingSession() {
  try {
    const me = await api("/api/me", "GET");
    if (me && me.email) {
      window.location.href = "/";
    }
  } catch (e) {}
}

document.querySelectorAll("[data-auth-tab]").forEach((btn) => {
  btn.addEventListener("click", () => {
    setAuthTab(btn.dataset.authTab);
  });
});

$("btnEntrarPage")?.addEventListener("click", async () => {
  const toast = $("toastLoginPage");
  hideToast(toast);

  try {
    const email = String($("loginEmailPage")?.value || "")
      .trim()
      .toLowerCase();
    const senha = String($("loginSenhaPage")?.value || "");

    if (!email) throw new Error("Informe seu e-mail.");
    if (!senha) throw new Error("Informe sua senha.");

    await api("/api/login", "POST", { email, senha });

    showToast(toast, "ok", "Login realizado", "Redirecionando...");
    setTimeout(() => {
      window.location.href = "/";
    }, 500);
  } catch (e) {
    showToast(toast, "err", "Falha no login", e.message);
  }
});

$("btnCriarContaPage")?.addEventListener("click", async () => {
  const toast = $("toastRegisterPage");
  hideToast(toast);

  try {
    const nome_apelido = String($("regApelidoPage")?.value || "").trim();
    const nome_completo = String($("regNomeCompletoPage")?.value || "").trim();
    const telefone = String($("regTelefonePage")?.value || "").trim();
    const email = String($("regEmailPage")?.value || "")
      .trim()
      .toLowerCase();
    const senha = String($("regSenhaPage")?.value || "");
    const confirmar_senha = String($("regConfPage")?.value || "");

    if (!email) throw new Error("Informe seu e-mail.");
    if (!senha) throw new Error("Informe sua senha.");
    if (!confirmar_senha) throw new Error("Confirme sua senha.");

    await api("/api/register", "POST", {
      nome_apelido,
      nome_completo,
      telefone,
      email,
      senha,
      confirmar_senha,
    });

    showToast(toast, "ok", "Conta criada", "Redirecionando...");
    setTimeout(() => {
      window.location.href = "/";
    }, 600);
  } catch (e) {
    showToast(toast, "err", "Erro ao cadastrar", e.message);
  }
});

$("btnResetarPage")?.addEventListener("click", async () => {
  const toast = $("toastResetPage");
  hideToast(toast);

  try {
    const email = String($("rstEmailPage")?.value || "")
      .trim()
      .toLowerCase();
    const nova_senha = String($("rstSenhaPage")?.value || "");
    const confirmar = String($("rstConfPage")?.value || "");

    if (!email) throw new Error("Informe seu e-mail.");
    if (!nova_senha) throw new Error("Informe a nova senha.");
    if (!confirmar) throw new Error("Confirme a nova senha.");

    await api("/api/reset_password", "POST", {
      email,
      nova_senha,
      confirmar,
    });

    showToast(
      toast,
      "ok",
      "Senha alterada",
      "Agora você já pode entrar com a nova senha."
    );

    $("loginEmailPage").value = email;
    $("loginSenhaPage").value = "";
    setAuthTab("entrar");
  } catch (e) {
    showToast(toast, "err", "Falha no reset", e.message);
  }
});

$("loginSenhaPage")?.addEventListener("keydown", async (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    await $("btnEntrarPage")?.click();
  }
});

$("regConfPage")?.addEventListener("keydown", async (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    await $("btnCriarContaPage")?.click();
  }
});

$("rstConfPage")?.addEventListener("keydown", async (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    await $("btnResetarPage")?.click();
  }
});

setAuthTab("entrar");
checkExistingSession();
