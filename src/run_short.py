"""
放空篩選執行入口:掃全市場,把「乖離背離放空」訊號結果寫成 docs/short_results.json

輸出格式比照現有 docs/results.json:保留最近5天的歷史紀錄,每天一筆,
前端可以切換日期查看。跟 run.py / results.json(多單篩選)是分開的獨立檔案。
"""
from __future__ import annotations
import sys
import os
import json
from datetime import datetime, date

sys.path.insert(0, os.path.dirname(__file__))
from universe import get_universe
from screener_short import (
    scan_universe_short,
    BIAS_MA_PERIOD, REF_LOOKBACK_DAYS, REF_BIAS_THRESHOLD,
    ATR_PERIOD, ATR_MIN_THRESHOLD,
)

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "short_results.json")
MAX_HISTORY_DAYS = 5


def main():
    print("抓取股票清單...")
    universe = get_universe(include_otc=True)
    print(f"共 {len(universe)} 檔,開始掃描乖離背離放空訊號...")

    results = scan_universe_short(universe)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today_str = date.today().strftime("%Y-%m-%d")

    new_entry = {
        "date": today_str,
        "generated_at": now_str,
        "params": {
            "bias_ma": BIAS_MA_PERIOD,
            "ref_lookback_days": REF_LOOKBACK_DAYS,
            "ref_bias_threshold": REF_BIAS_THRESHOLD,
            "atr_period": ATR_PERIOD,
            "atr_min_threshold": ATR_MIN_THRESHOLD,
        },
        "count": len(results),
        "results": results,
    }

    # 讀取舊資料(如果有),把今天的結果放最前面,同一天重跑就覆蓋掉舊的
    history = []
    if os.path.exists(OUTPUT_PATH):
        try:
            with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
                old = json.load(f)
                history = old.get("history", [])
        except Exception:
            history = []

    history = [h for h in history if h.get("date") != today_str]
    history.insert(0, new_entry)
    history = history[:MAX_HISTORY_DAYS]

    output = {
        "generated_at": now_str,
        "history": history,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"完成,共 {len(results)} 檔觸發訊號,已寫入 {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
