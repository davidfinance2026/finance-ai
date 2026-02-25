# Finance AI (Railway + PWA)

## Deploy no Railway (via GitHub)
1. Suba este projeto para um repositório no GitHub.
2. No Railway: **New Project** → **Deploy from GitHub Repo**.
3. Em **Variables**, configure:
   - `SERVICE_ACCOUNT_JSON` = JSON completo da conta de serviço (em uma linha)
   - `SECRET_KEY` = qualquer string

## PWA (instalar como app)
- O app já vem com:
  - `/static/manifest.json`
  - `/static/sw.js` (cache offline básico)
  - ícones em `/static/icons/`
- No Android/Chrome: menu ⋮ → **Adicionar à tela inicial**.

## Google Sheets
- Planilha: **Controle Financeiro**
- Abas:
  - **Usuarios**
  - **Lancamentos**
O app cria/ajusta os cabeçalhos automaticamente se necessário.
