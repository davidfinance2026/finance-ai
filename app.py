from datetime import datetime

from flask import Flask, request, jsonify

app = Flask(__name__)

def parse_data_br(s: str):
    # exemplo esperado: "19/02/2026"
    return datetime.strptime(s.strip(), "%d/%m/%Y").date()

def parse_float(v):
    # aceita 120, "120", "120,50"
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    v = str(v).strip().replace(".", "").replace(",", ".")
    try:
        return float(v)
    except:
        return 0.0

# ... sua função get_sheet() aqui ...

@app.get("/resumo")
def resumo():
    """
    /resumo?mes=YYYY-MM  (ex: 2026-02)
    Se não passar mes, usa o mês atual.
    """
    mes = request.args.get("mes")
    hoje = datetime.now().date()
    if not mes:
        mes = hoje.strftime("%Y-%m")

    ano, mm = mes.split("-")
    ano = int(ano)
    mm = int(mm)

    sh = get_sheet()
    dados = sh.get_all_records()  # lista de dicts

    def pick(row, *keys):
        for k in keys:
            if k in row:
                return row.get(k)
        return None

    do_mes = []
    for r in dados:
        data_txt = pick(r, "Data", "data")
        if not data_txt:
            continue
        try:
            d = parse_data_br(str(data_txt))
        except:
            continue

        if d.year == ano and d.month == mm:
            tipo = str(pick(r, "Tipo", "tipo") or "").strip().lower()
            valor = parse_float(pick(r, "Valor", "valor"))
            categoria = pick(r, "Categoria", "categoria") or ""
            descricao = pick(r, "Descrição", "Descricao", "descricao") or ""

            do_mes.append({
                "data": d.strftime("%d/%m/%Y"),
                "tipo": "Receita" if "rece" in tipo else "Gasto",
                "categoria": str(categoria),
                "descricao": str(descricao),
                "valor": valor,
            })

    entradas = sum(x["valor"] for x in do_mes if x["tipo"] == "Receita")
    saidas   = sum(x["valor"] for x in do_mes if x["tipo"] == "Gasto")
    saldo    = entradas - saidas

    por_dia = {}
    for x in do_mes:
        dia = x["data"][:2]  # "dd"
        por_dia.setdefault(dia, {"receita": 0.0, "gasto": 0.0})
        if x["tipo"] == "Receita":
            por_dia[dia]["receita"] += x["valor"]
        else:
            por_dia[dia]["gasto"] += x["valor"]

    dias = sorted(por_dia.keys(), key=lambda z: int(z))
    serie_receita = [por_dia[d]["receita"] for d in dias]
    serie_gasto   = [por_dia[d]["gasto"] for d in dias]

    return jsonify({
        "mes": mes,
        "entradas": entradas,
        "saidas": saidas,
        "saldo": saldo,
        "dias": dias,
        "serie_receita": serie_receita,
        "serie_gasto": serie_gasto,
        "ultimos": do_mes[-10:],
        "qtd": len(do_mes)
    })
