
async function atualizarDashboard(){

const r = await fetch("/api/dashboard")

if(!r.ok) return

const d = await r.json()

document.getElementById("valReceitas").innerText =
d.receitas_formatado

document.getElementById("valGastos").innerText =
d.gastos_formatado

document.getElementById("valSaldo").innerText =
d.saldo_formatado

}

document
.getElementById("btnAtualizarDash")
.onclick = atualizarDashboard

async function carregarUsuario(){

const r = await fetch("/api/me")

if(!r.ok) return

const u = await r.json()

if(!u.email) return

document
.getElementById("accountEmail")
innerText = u.email

let nome = u.name || u.email.split("@")[0]

document
.getElementById("helloTitle")
innerText = "Boa noite, " + nome + " 👋"

}

carregarUsuario()
atualizarDashboard()
