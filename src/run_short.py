"""
放空篩選執行入口:掃全市場,把「乖離背離放空」訊號結果寫成 docs/short_results.json

跟現有 run.py 是分開的獨立流程,不會動到 docs/results.json(多單篩選結果)。
"""
from __future__ import annotations
import sys
import os
import json
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from universe import get_universe
from screener_short import scan_universe_short

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "short_results.json")


def main():
    print("抓取股票清單...")
    universe = get_universe(include_otc=True)
    print(f"共 {len(universe)} 檔,開始掃描乖離背離放空訊號...")

    results = scan_universe_short(universe)

    output = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(results),
        "results": results,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"完成,共 {len(results)} 檔觸發訊號,已寫入 {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
