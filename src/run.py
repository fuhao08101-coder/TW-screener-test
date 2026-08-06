"""
每日執行入口:抓清單 -> 掃描 -> 輸出 docs/results.json (給手機網頁讀取)
本機測試: python src/run.py
GitHub Actions 會每天自動跑這支

【新增】保留最近 MAX_HISTORY_DAYS 個交易日的結果,存成 history 陣列,
        同一天重複執行會覆蓋掉當天那筆,不會重複累積。
"""
import json
import sys
import os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))

from universe import get_universe
from screener import (
    scan_universe,
    LOOKBACK_DAYS,
    BIAS_MA_PERIOD,
    BIAS_THRESHOLD,
    LONG_MA_PERIOD,
    MA87_BREACH_LOOKBACK,
    SECOND_MA_PERIOD,
    REQUIRE_MA_ALIGNMENT,
)

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "results.json")
MAX_HISTORY_DAYS = 5


def load_existing_history() -> list[dict]:
    """讀取現有的 results.json,拿出 history 陣列。檔案不存在或格式不對就回傳空清單。"""
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

    tz = timezone(timedelta(hours=8))  # 台北時間
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
        },
        "count": len(results),
        "results": results,
    }

    history = load_existing_history()
    # 如果今天已經跑過一次(同一天重跑),先把舊的今天那筆移除,避免重複
    history = [h for h in history if h.get("date") != today_str]
    history.insert(0, new_entry)          # 新的一天放最前面
    history = history[:MAX_HISTORY_DAYS]  # 只保留最近5筆

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
