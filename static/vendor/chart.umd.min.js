import os
import urllib.request
from pathlib import Path

CHART_URL = "https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"
OUT_PATH = Path("static/vendor/chart.umd.min.js")

def main():
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    print("Baixando Chart.js…")
    with urllib.request.urlopen(CHART_URL, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Falha ao baixar. HTTP {resp.status}")
        data = resp.read()

    if not data or len(data) < 50_000:
        # Chart.js min geralmente é bem maior que isso; sanity check
        raise RuntimeError("Arquivo baixado parece pequeno demais. Abortando por segurança.")

    OUT_PATH.write_bytes(data)

    size_kb = OUT_PATH.stat().st_size / 1024
    print(f"OK ✅ Salvo em: {OUT_PATH} ({size_kb:.1f} KB)")

if __name__ == "__main__":
    main()
