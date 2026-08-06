"""
每日執行入口:抓清單 -> 掃描 -> 輸出 docs/results.json (給手機網頁讀取)
本機測試: python src/run.py
GitHub Actions 會每天自動跑這支
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


def main():
    print("抓取股票清單...")
    universe = get_universe(include_otc=True)
    print(f"共 {len(universe)} 檔,開始掃描...")

    results = scan_universe(universe)
    print(f"符合條件: {len(results)} 檔")

    tz = timezone(timedelta(hours=8))  # 台北時間
    payload = {
        "generated_at": datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S"),
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

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"已輸出至 {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
