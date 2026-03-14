// ------------------------------------
// Finance AI - Login JS
// ------------------------------------

function showToast(el, msg, ok=true){
  if(!el) return
  el.textContent = msg
  el.className = "toast show " + (ok ? "ok":"err")
  setTimeout(()=> el.className="toast",4000)
}

// ------------------------------------
// Tabs
// ------------------------------------

document.querySelectorAll("[data-auth-tab]").forEach(tab=>{
  tab.addEventListener("click",()=>{

    document.querySelectorAll("[data-auth-tab]").forEach(t=>t.classList.remove("active"))
    tab.classList.add("active")

    const name = tab.dataset.authTab

    document.querySelectorAll(".login-tab-panel").forEach(p=>p.classList.add("hidden"))

    document.getElementById("login-tab-"+name).classList.remove("hidden")
  })
})


// ------------------------------------
// LOGIN
// ------------------------------------

const btnLogin = document.getElementById("btnEntrarPage")

if(btnLogin){

btnLogin.onclick = async ()=>{

  const email = document.getElementById("loginEmailPage").value.trim()
  const senha = document.getElementById("loginSenhaPage").value.trim()

  const toast = document.getElementById("toastLoginPage")

  if(!email || !senha){
    showToast(toast,"Preencha email e senha",false)
    return
  }

  try{

    const r = await fetch("/api/login",{
      method:"POST",
      headers:{ "Content-Type":"application/json" },
      body: JSON.stringify({
        email:email,
        senha:senha
      })
    })

    const j = await r.json()

    if(!r.ok){
      showToast(toast,j.error || "Erro ao entrar",false)
      return
    }

    showToast(toast,"Login realizado com sucesso")

    setTimeout(()=>{
      window.location="/"
    },800)

  }catch(e){
    showToast(toast,"Erro de conexão",false)
  }

}
}


// ------------------------------------
// REGISTER
// ------------------------------------

const btnRegister = document.getElementById("btnCriarContaPage")

if(btnRegister){

btnRegister.onclick = async ()=>{

  const nomeCompleto = document.getElementById("regNomeCompletoPage").value.trim()
  const apelido = document.getElementById("regApelidoPage").value.trim()

  const email = document.getElementById("regEmailPage").value.trim()

  const senha = document.getElementById("regSenhaPage").value.trim()
  const confirmar = document.getElementById("regConfPage").value.trim()

  const toast = document.getElementById("toastRegisterPage")

  if(!email || !senha){
    showToast(toast,"Preencha email e senha",false)
    return
  }

  try{

    const r = await fetch("/api/register",{
      method:"POST",
      headers:{ "Content-Type":"application/json" },
      body: JSON.stringify({
        nome_completo: nomeCompleto,
        nome: apelido,
        email: email,
        senha: senha,
        confirmar_senha: confirmar
      })
    })

    const j = await r.json()

    if(!r.ok){
      showToast(toast,j.error || "Erro ao criar conta",false)
      return
    }

    showToast(toast,"Conta criada com sucesso")

    setTimeout(()=>{
      window.location="/"
    },800)

  }catch(e){
    showToast(toast,"Erro de conexão",false)
  }

}
}


// ------------------------------------
// RESET PASSWORD
// ------------------------------------

const btnReset = document.getElementById("btnResetarPage")

if(btnReset){

btnReset.onclick = async ()=>{

  const email = document.getElementById("rstEmailPage").value.trim()

  const senha = document.getElementById("rstSenhaPage").value.trim()
  const confirmar = document.getElementById("rstConfPage").value.trim()

  const toast = document.getElementById("toastResetPage")

  if(!email || !senha){
    showToast(toast,"Preencha os campos",false)
    return
  }

  try{

    const r = await fetch("/api/reset_password",{
      method:"POST",
      headers:{ "Content-Type":"application/json" },
      body: JSON.stringify({
        email: email,
        nova_senha: senha,
        confirmar: confirmar
      })
    })

    const j = await r.json()

    if(!r.ok){
      showToast(toast,j.error || "Erro ao resetar senha",false)
      return
    }

    showToast(toast,"Senha alterada com sucesso")

  }catch(e){
    showToast(toast,"Erro de conexão",false)
  }

}
}
