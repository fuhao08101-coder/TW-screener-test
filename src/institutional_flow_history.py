"""
外資+融資歷史資料抓取,專門給回測用(不是即時篩選)。

沿用 institutional_flow.py 裡的低階函式(_fetch_t86_twse / _fetch_margin_twse /
_fetch_t86_tpex / _fetch_margin_tpex),差別是這支會往回抓半年份(約125個交易日+9天緩衝)
的歷史,而不是只抓最近9天。上市+上櫃合併在一起。
"""
from __future__ import annotations
import time
from datetime import date, timedelta

from institutional_flow import (
    _fetch_t86_twse, _fetch_margin_twse,
    _fetch_t86_tpex, _fetch_margin_tpex,
)

REQUEST_SLEEP = 0.3
TRADING_DAYS_NEEDED = 134   # 125個交易日(半年回測範圍)+9天緩衝(給雙買濾網lookback用)
MAX_CALENDAR_DAYS_TO_SCAN = 230


def fetch_institutional_history(trading_days_needed: int = TRADING_DAYS_NEEDED):
    """
    往回抓 trading_days_needed 個交易日的外資+融資資料,上市+上櫃合併在一起。
    回傳 [(date_str, foreign_map, margin_map), ...],由舊到新排列。
    """
    collected = []
    d = date.today()
    tried = 0
    while len(collected) < trading_days_needed and tried < MAX_CALENDAR_DAYS_TO_SCAN:
        date_str_twse = d.strftime("%Y%m%d")

        foreign_twse = _fetch_t86_twse(date_str_twse)
        time.sleep(REQUEST_SLEEP)
        margin_twse = _fetch_margin_twse(date_str_twse) if foreign_twse is not None else None
        time.sleep(REQUEST_SLEEP)

        foreign_tpex = _fetch_t86_tpex(d)
        time.sleep(REQUEST_SLEEP)
        margin_tpex = _fetch_margin_tpex(d) if foreign_tpex is not None else None
        time.sleep(REQUEST_SLEEP)

        foreign_map = {}
        margin_map = {}
        if foreign_twse is not None and margin_twse is not None:
            foreign_map.update(foreign_twse)
            margin_map.update(margin_twse)
        if foreign_tpex is not None and margin_tpex is not None:
            foreign_map.update(foreign_tpex)
            margin_map.update(margin_tpex)

        if foreign_map and margin_map:
            collected.append((date_str_twse, foreign_map, margin_map))

        d -= timedelta(days=1)
        tried += 1
        if tried % 20 == 0:
            print(f"  ...已嘗試 {tried} 個日曆天,目前收集到 {len(collected)} 個交易日")

    collected.reverse()  # 由舊到新
    print(f"外資融資歷史資料:實際取得 {len(collected)} 個交易日(目標{trading_days_needed}天)")
    return collected


def compute_daily_qualified_sets(daily_data, window: int = 9) -> dict[str, set[str]]:
    """
    對 daily_data 裡從第 window 天開始的每一天,計算「當天合格」的股票代號集合:
    近 window 個交易日內曾經外資+融資同天雙買,且雙買之後到當天為止沒有出現過同天雙減。
    回傳 {date_str: set(codes)}
    """
    result: dict[str, set[str]] = {}

    for i in range(window - 1, len(daily_data)):
        window_slice = daily_data[i - window + 1:i + 1]

        all_codes: set[str] = set()
        for _, foreign_map, margin_map in window_slice:
            all_codes.update(foreign_map.keys())
            all_codes.update(margin_map.keys())

        qualified: set[str] = set()
        for code in all_codes:
            trigger_idx = None
            for idx, (_, foreign_map, margin_map) in enumerate(window_slice):
                foreign_net = foreign_map.get(code, 0)
                margin_chg = margin_map.get(code, 0)
                if foreign_net > 0 and margin_chg > 0:
                    trigger_idx = idx
                    break
            if trigger_idx is None:
                continue

            has_dual_sell_after = False
            for idx in range(trigger_idx, len(window_slice)):
                _, foreign_map, margin_map = window_slice[idx]
                foreign_net = foreign_map.get(code, 0)
                margin_chg = margin_map.get(code, 0)
                if foreign_net < 0 and margin_chg < 0:
                    has_dual_sell_after = True
                    break

            if not has_dual_sell_after:
                qualified.add(code)

        date_str = daily_data[i][0]
        result[date_str] = qualified

    return result


def build_qualified_dates_by_code(window: int = 9) -> dict[str, set[str]]:
    """
    一次抓好歷史資料+算好每日合格名單,轉成「每檔股票在哪些日期合格」的形式,
    方便回測時直接用 code 查詢。回傳 {股票代號: {合格的date_str, ...}}
    """
    daily_data = fetch_institutional_history()
    if len(daily_data) < window:
        print("[warn] 外資融資歷史資料不足,回測這次不會有任何股票通過雙買濾網")
        return {}

    qualified_by_date = compute_daily_qualified_sets(daily_data, window=window)

    qualified_dates_by_code: dict[str, set[str]] = {}
    for date_str, codes in qualified_by_date.items():
        for code in codes:
            qualified_dates_by_code.setdefault(code, set()).add(date_str)

    print(f"共計算出 {len(qualified_by_date)} 個交易日的合格名單,涵蓋 {len(qualified_dates_by_code)} 檔股票曾經合格過")
    return qualified_dates_by_code
