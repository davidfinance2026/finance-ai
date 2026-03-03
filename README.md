# FinanceAI (Flask + WhatsApp + Railway)

Inclui:
- A) 📊 Resumos inteligentes / Consolidados (API + comando WhatsApp `resumo`)
- C) 🔁 Recorrentes (API + comando WhatsApp `recorrentes` / `rodar recorrentes`)
- D) 🧠 Inteligência analítica (API + comando WhatsApp `analise`)

## Variáveis de ambiente (Railway)
Obrigatórias:
- DATABASE_URL
- SECRET_KEY

WhatsApp Cloud API:
- WA_VERIFY_TOKEN
- WA_ACCESS_TOKEN
- WA_PHONE_NUMBER_ID
- GRAPH_VERSION (opcional, padrão v20.0)

Opcional:
- PANIC_TOKEN (se definir, protege `/api/recorrentes/run` e `/api/panic_reset`)

## Rotas novas
- GET  /api/consolidados?mes=3&ano=2026
- GET  /api/resumos_inteligentes?mes=3&ano=2026
- GET  /api/analitico?mes=3&ano=2026
- GET  /api/recorrentes
- POST /api/recorrentes
- PUT  /api/recorrentes/<id>
- DEL  /api/recorrentes/<id>
- POST /api/recorrentes/run

## Cron no Railway (recomendado)
Crie um Scheduled Job (ou Cron) chamando:
POST https://SEU_DOMINIO/api/recorrentes/run?token=SEU_PANIC_TOKEN
ou envie header `X-Panic-Token: SEU_PANIC_TOKEN`.

Sugestão: 1x ao dia (ex: 09:00) ou 1x por hora.

## Comandos WhatsApp
- ajuda
- resumo  (ou `resumo 03/2026`)
- analise (ou `analise 03/2026`)
- recorrentes
- rodar recorrentes
- ultimos / editar / apagar / corrigir ultima
