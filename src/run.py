"""
每日執行入口:抓清單 -> 掃描 -> 輸出 docs/results.json (給手機網頁讀取)
本機測試: python src/run.py
GitHub Actions 會每天自動跑這支

保留最近 MAX_HISTORY_DAYS 個交易日的結果,存成 history 陣列,
同一天重複執行會覆蓋掉當天那筆,不會重複累積。
"""
import json
import sys
import os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))

from universe import get_universe
from revenue import get_monthly_revenue
from screener import (
    scan_universe,
    LOOKBACK_DAYS,
    BIAS_MA_PERIOD,
    BIAS_THRESHOLD,
    LONG_MA_PERIOD,
    MA87_BREACH_LOOKBACK,
    SECOND_MA_PERIOD,
    REQUIRE_MA_ALIGNMENT,
    ATR_PERIOD,
    ATR_MIN_THRESHOLD,
    ATR_MIN_PCT_THRESHOLD,
    REQUIRE_ATR_MIN,
)

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "results.json")
MAX_HISTORY_DAYS = 5


def load_existing_history() -> list[dict]:
    if not os.path.exists(OUTPUT_PATH):
        return []
    try:
        with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
            old = json.load(f)
        return old.get("history", [])
    except Exception:
        return []


def main():
    print("抓取股票清單...")
    universe = get_universe(include_otc=True)
    print(f"共 {len(universe)} 檔,開始掃描...")

    results = scan_universe(universe)
    print(f"符合條件: {len(results)} 檔")

    print("抓取月營收資料...")
    revenue_map = get_monthly_revenue()
    print(f"取得 {len(revenue_map)} 家公司月營收資料(僅上市TWSE)")

    for r in results:
        code = r["ticker"].replace(".TWO", "").replace(".TW", "")
        rev = revenue_map.get(code)
        if rev:
            r["revenue_month"] = rev["month"]
            r["revenue_mom_pct"] = round(rev["mom_pct"], 2) if rev["mom_pct"] is not None else None
            r["revenue_yoy_pct"] = round(rev["yoy_pct"], 2) if rev["yoy_pct"] is not None else None
        else:
            r["revenue_month"] = None
            r["revenue_mom_pct"] = None
            r["revenue_yoy_pct"] = None

    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    today_str = now.strftime("%Y-%m-%d")

    new_entry = {
        "date": today_str,
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "params": {
            "lookback_days": LOOKBACK_DAYS,
            "bias_ma": BIAS_MA_PERIOD,
            "bias_threshold": BIAS_THRESHOLD,
            "long_ma": LONG_MA_PERIOD,
            "ma87_breach_lookback": MA87_BREACH_LOOKBACK,
            "second_ma": SECOND_MA_PERIOD,
            "ma_alignment": REQUIRE_MA_ALIGNMENT,
            "atr_period": ATR_PERIOD,
            "atr_min_threshold": ATR_MIN_THRESHOLD,
            "atr_min_pct_threshold": ATR_MIN_PCT_THRESHOLD,
            "require_atr_min": REQUIRE_ATR_MIN,
        },
        "count": len(results),
        "results": results,
    }

    history = load_existing_history()
    history = [h for h in history if h.get("date") != today_str]
    history.insert(0, new_entry)
    history = history[:MAX_HISTORY_DAYS]

    payload = {
        "generated_at": new_entry["generated_at"],
        "history": history,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"已輸出至 {OUTPUT_PATH}(共保留 {len(history)} 天歷史)")


if __name__ == "__main__":
    main()
