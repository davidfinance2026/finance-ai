from pathlib import Path

app_file = Path("app.py")
index_file = Path("templates/index.html")

app_code = app_file.read_text()
index_code = index_file.read_text()

if "api_score_financeiro" not in app_code:

    app_code += """

@app.route("/api/score_financeiro")
def api_score_financeiro():
    from flask import session
    user_id = session.get("user_id")
    if not user_id:
        return {"error": "not logged"}, 401

    conn = get_db()
    cur = conn.cursor()

    cur.execute(\"\"\"
        SELECT
        SUM(CASE WHEN tipo='RECEITA' THEN valor ELSE 0 END),
        SUM(CASE WHEN tipo='GASTO' THEN valor ELSE 0 END)
        FROM transactions
        WHERE user_id=%s
    \"\"\", (user_id,))

    receitas, gastos = cur.fetchone()

    receitas = receitas or 0
    gastos = gastos or 0
    saldo = receitas - gastos

    score = 50
    if saldo > 0:
        score += 20
    if receitas > gastos:
        score += 15
    if receitas > 0:
        score += 15

    score = min(score,100)

    return {
        "score": score,
        "receitas": receitas,
        "gastos": gastos,
        "saldo": saldo
    }

"""

    app_file.write_text(app_code)

if "valScore" not in index_code:

    insert_card = """

<div class="miniCard">
<div class="row">
<div class="muted" style="font-weight:900">SCORE FINANCEIRO</div>
<div class="spacer"></div>
<div id="valScore" class="kpi">0/100</div>
</div>
<div class="hint">Saúde financeira</div>
</div>

"""

    index_code = index_code.replace(
        "INSIGHTS DO MÊS",
        "INSIGHTS DO MÊS" + insert_card
    )

    index_file.write_text(index_code)

print("Atualização aplicada com sucesso.")
