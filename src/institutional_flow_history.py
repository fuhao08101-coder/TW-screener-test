"""
外資+融資歷史資料抓取,專門給回測用(不是即時篩選)。

沿用 institutional_flow.py 裡的低階函式(_fetch_t86_twse / _fetch_margin_twse /
_fetch_t86_tpex / _fetch_margin_tpex)。上市+上櫃合併在一起。

v15更新重點:
  1. trading_days_needed / max_calendar_days_to_scan 改成可傳入參數,
     v14呼叫時不帶參數則沿用原本半年(134個交易日)的預設值,行為不變;
     v15呼叫時會帶入約3年(750個交易日)的目標,不影響v14。
  2. 加上本地JSON快取(data/institutional_flow_cache.json):
     每一天的外資/融資原始資料(或「這天查無資料」的記錄)都會存檔,
     已經抓過的日期下次執行不會重複打API。這是為了讓「拉長回測年限」
     這種需要大量歷史請求的操作,能夠中途失敗後從快取處續跑,不用整個重抓,
     也讓v14、v15共用同一份快取,越用越快。
"""
from __future__ import annotations
import json
import os
import time
from datetime import date, timedelta

from institutional_flow import (
    _fetch_t86_twse, _fetch_margin_twse,
    _fetch_t86_tpex, _fetch_margin_tpex,
)

REQUEST_SLEEP = 0.3
TRADING_DAYS_NEEDED = 134   # v14預設值(半年回測範圍+9天緩衝),不要動,保持v14行為不變
MAX_CALENDAR_DAYS_TO_SCAN = 230  # 對應上面134天的日曆天掃描上限,v14預設值

# 快取檔案位置:src/的上一層 data/ 資料夾
DEFAULT_CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "institutional_flow_cache.json")
SAVE_EVERY_N_NEW_DAYS = 20  # 每新抓20天就存一次檔,避免長時間執行中途失敗要整個重來


def _load_cache(cache_path: str) -> dict:
    if not os.path.exists(cache_path):
        return {}
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[warn] 讀取快取失敗({cache_path}): {e},視為沒有快取,重新抓取")
        return {}


def _save_cache(cache_path: str, cache: dict) -> None:
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    tmp_path = cache_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    os.replace(tmp_path, cache_path)  # 原子寫入,避免存到一半被中斷產生壞檔


def fetch_institutional_history(
    trading_days_needed: int = TRADING_DAYS_NEEDED,
    max_calendar_days_to_scan: int = MAX_CALENDAR_DAYS_TO_SCAN,
    cache_path: str = DEFAULT_CACHE_PATH,
    use_cache: bool = True,
):
    """
    往回抓 trading_days_needed 個交易日的外資+融資資料,上市+上櫃合併在一起。
    回傳 [(date_str, foreign_map, margin_map), ...],由舊到新排列。

    支援快取:cache[date_str] = {"foreign":..., "margin":...} 代表這天有資料;
    cache[date_str] = None 代表已確認這天查無資料(非交易日等),下次不會再重打API確認。
    """
    cache = _load_cache(cache_path) if use_cache else {}
    new_days_since_save = 0

    collected = []
    d = date.today()
    tried = 0
    while len(collected) < trading_days_needed and tried < max_calendar_days_to_scan:
        date_str_twse = d.strftime("%Y%m%d")

        if use_cache and date_str_twse in cache:
            entry = cache[date_str_twse]
            if entry is not None:
                collected.append((date_str_twse, entry["foreign"], entry["margin"]))
            # entry為None代表快取過「這天沒資料」,直接跳過,不打API
        else:
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
                if use_cache:
                    cache[date_str_twse] = {"foreign": foreign_map, "margin": margin_map}
                collected.append((date_str_twse, foreign_map, margin_map))
            elif use_cache:
                cache[date_str_twse] = None

            if use_cache:
                new_days_since_save += 1
                if new_days_since_save >= SAVE_EVERY_N_NEW_DAYS:
                    _save_cache(cache_path, cache)
                    new_days_since_save = 0
                    print(f"  ...已存檔快取,快取累計 {len(cache)} 天,目前收集到 {len(collected)}/{trading_days_needed} 個交易日")

        d -= timedelta(days=1)
        tried += 1
        if tried % 50 == 0:
            print(f"  ...已嘗試 {tried} 個日曆天,目前收集到 {len(collected)} 個交易日")

    if use_cache and new_days_since_save > 0:
        _save_cache(cache_path, cache)
        print(f"  ...最終存檔,快取累計 {len(cache)} 天")

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


def build_qualified_dates_by_code(
    window: int = 9,
    trading_days_needed: int = TRADING_DAYS_NEEDED,
    max_calendar_days_to_scan: int = MAX_CALENDAR_DAYS_TO_SCAN,
    cache_path: str = DEFAULT_CACHE_PATH,
    use_cache: bool = True,
) -> dict[str, set[str]]:
    """
    一次抓好歷史資料+算好每日合格名單,轉成「每檔股票在哪些日期合格」的形式,
    方便回測時直接用 code 查詢。回傳 {股票代號: {合格的date_str, ...}}

    trading_days_needed / max_calendar_days_to_scan 不帶入時沿用v14的半年預設值,
    v15呼叫時會帶入約3年的目標值,兩者互不影響。
    """
    daily_data = fetch_institutional_history(
        trading_days_needed=trading_days_needed,
        max_calendar_days_to_scan=max_calendar_days_to_scan,
        cache_path=cache_path,
        use_cache=use_cache,
    )
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
