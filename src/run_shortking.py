"""
短線王:每日掃描組合C進場訊號(收盤站上5日高+乖離8%+ATR14>=8+大盤環境濾網),
附加月營收MoM/YoY、相同族群標籤、鉅額交易標籤,以及大盤(加權/櫃買)強弱狀態。
輸出 docs/shortking_results.json
用法(手動觸發): python src/run_shortking.py
"""
import json
import sys
import os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))

from universe import get_universe
from revenue import get_monthly_revenue
from block_trade import check_batch
from shortking_screener import (
    scan_universe,
    ATR_PERIOD,
    ATR_MIN_THRESHOLD,
    SHORT_MA_PERIOD,
    BIAS_MIN_THRESHOLD,
    BREAKOUT_LOOKBACK_DAYS,
    TOP_N_PER_DAY,
)

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "shortking_results.json")
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

    twse_count = sum(1 for r in universe if r["market"] == "TWSE")
    tpex_count = sum(1 for r in universe if r["market"] == "TPEX")
    print(f"上市: {twse_count} 檔, 上櫃: {tpex_count} 檔")

    MIN_EXPECTED_TWSE = 500
    MIN_EXPECTED_TPEX = 300
    if twse_count < MIN_EXPECTED_TWSE or tpex_count < MIN_EXPECTED_TPEX:
        print(f"❌ 股票清單異常過少,放棄這次更新,保留前一天的結果。")
        return

    print("開始掃描短線王訊號(組合C+大盤濾網+雙買+前6名乖離)...")
    results, market_regime = scan_universe(universe)
    print(f"符合條件: {len(results)} 檔")

    print("抓取月營收資料...")
    revenue_map = get_monthly_revenue()
    print(f"取得 {len(revenue_map)} 家公司月營收資料")

    for r in results:
        code = r["ticker"].replace(".TWO", "").replace(".TW", "")
        rev = revenue_map.get(code)
        if rev:
            r["revenue_month"] = rev["month"]
            r["industry"] = rev.get("industry")
            r["revenue_mom_pct"] = round(rev["mom_pct"], 2) if rev["mom_pct"] is not None else None
            r["revenue_yoy_pct"] = round(rev["yoy_pct"], 2) if rev["yoy_pct"] is not None else None
        else:
            r["revenue_month"] = None
            r["industry"] = None
            r["revenue_mom_pct"] = None
            r["revenue_yoy_pct"] = None

    # 相同族群標籤
    industry_counts = {}
    for r in results:
        ind = r.get("industry")
        if ind:
            industry_counts[ind] = industry_counts.get(ind, 0) + 1
    for r in results:
        ind = r.get("industry")
        r["same_group_industry"] = ind if (ind and industry_counts.get(ind, 0) >= 2) else None

    print("檢查近3個月鉅額交易紀錄(僅上市TWSE)...")
    twse_codes = [
        r["ticker"].replace(".TWO", "").replace(".TW", "")
        for r in results if r["market"] == "TWSE"
    ]
    block_trade_map = check_batch(twse_codes)
    for r in results:
        code = r["ticker"].replace(".TWO", "").replace(".TW", "")
        r["has_block_trade"] = block_trade_map.get(code)

    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    today_str = now.strftime("%Y-%m-%d")

    new_entry = {
        "date": today_str,
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "params": {
            "atr_period": ATR_PERIOD,
            "atr_min_threshold": ATR_MIN_THRESHOLD,
            "short_ma": SHORT_MA_PERIOD,
            "bias_min_threshold": BIAS_MIN_THRESHOLD,
            "breakout_lookback_days": BREAKOUT_LOOKBACK_DAYS,
            "top_n_per_day": TOP_N_PER_DAY,
            "require_dual_buy": True,
        },
        "market_regime": market_regime,
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
