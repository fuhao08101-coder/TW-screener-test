"""
權證選擇器(第一層篩選,不需要即時報價):
  對指定的標的股票代號,從所有掛在這檔股票下的權證裡,篩選出:
    1. 距離到期日還有一個月以上(履約截止日 >= 今天 + 30天)
    2. 依「執行比例」由高到低排序(執行比例 = 每仟單位權證可換購的標的股數)

【第二層篩選(還沒做,需要富邦API的即時報價才能做)】:
  委買委賣張數(買200/賣10-499)、造市商掛單判斷、價差是否超過5個TICK,
  這些都是即時盤中報價才有的資訊,t187ap37_L這份靜態參考資料完全不包含,
  等富邦API串接好之後,才能在這裡篩選出來的候選清單上,再做一次即時篩選。

資料源:TWSE官方「上市權證基本資料」t187ap37_L(跟warrant_underlyings.py共用邏輯)
"""
import requests
from datetime import date, timedelta

URL = "https://openapi.twse.com.tw/v1/opendata/t187ap37_L"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

MIN_DAYS_TO_EXPIRY = 30  # 距離到期日至少要幾天以上


def _normalize_name(name: str) -> str:
    if not name:
        return ""
    name = name.strip()
    name = name.replace("　", "").replace(" ", "")
    name = name.replace("－", "-").replace("─", "-")
    return name


def _roc_to_date(roc_str: str) -> date | None:
    """民國年字串(例如"1150826")轉西元date物件"""
    if not roc_str or len(roc_str) != 7:
        return None
    try:
        roc_year = int(roc_str[:3])
        month = int(roc_str[3:5])
        day = int(roc_str[5:7])
        return date(roc_year + 1911, month, day)
    except (ValueError, TypeError):
        return None


def fetch_all_warrants(max_retries: int = 3) -> list[dict]:
    """抓取全部權證基本資料(原始格式)"""
    import time
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(URL, headers=HEADERS, timeout=60)
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            print(f"[warn] 權證資料第{attempt}次抓取失敗: {e}")
        if attempt < max_retries:
            time.sleep(3)
    print("[warn] 權證資料抓取失敗,回傳空清單")
    return []


def select_warrants_for_stock(stock_code: str, stock_name: str, all_warrants: list[dict],
                               min_days_to_expiry: int = MIN_DAYS_TO_EXPIRY) -> list[dict]:
    """
    對單一標的股票,從全部權證資料裡篩出符合條件的候選權證,依執行比例由高到低排序。
    回傳每筆:{warrant_code, warrant_name, warrant_type, expiry_date, exercise_ratio, days_to_expiry}
    """
    norm_target_name = _normalize_name(stock_name)
    today = date.today()
    cutoff = today + timedelta(days=min_days_to_expiry)

    candidates = []
    for row in all_warrants:
        underlying = _normalize_name(row.get("標的證券/指數", ""))
        if underlying != norm_target_name:
            continue

        expiry = _roc_to_date(row.get("履約截止日", ""))
        if expiry is None or expiry < cutoff:
            continue  # 距離到期日不足一個月,排除

        try:
            exercise_ratio = float(row.get("最新標的履約配發數量(每仟單位權證)", "0"))
        except (ValueError, TypeError):
            exercise_ratio = 0.0

        candidates.append({
            "warrant_code": row.get("權證代號", ""),
            "warrant_name": row.get("權證簡稱", ""),
            "warrant_type": row.get("權證類型", ""),  # 認購 / 認售
            "expiry_date": expiry.isoformat(),
            "days_to_expiry": (expiry - today).days,
            "exercise_ratio": exercise_ratio,
        })

    candidates.sort(key=lambda c: c["exercise_ratio"], reverse=True)
    return candidates


if __name__ == "__main__":
    import sys
    import os
    sys.path.insert(0, os.path.dirname(__file__))

    TEST_CODE = "2330"
    TEST_NAME = "台積電"

    print("抓取權證資料...")
    all_warrants = fetch_all_warrants()
    print(f"共 {len(all_warrants)} 檔權證")

    candidates = select_warrants_for_stock(TEST_CODE, TEST_NAME, all_warrants)
    print(f"\n{TEST_CODE}({TEST_NAME})符合條件(到期日一個月以上)的權證,"
          f"共 {len(candidates)} 檔,依執行比例由高到低列出前10檔:")
    for c in candidates[:10]:
        print(f"  {c['warrant_code']} {c['warrant_name']}({c['warrant_type']}) "
              f"執行比例{c['exercise_ratio']} 到期{c['expiry_date']}(還有{c['days_to_expiry']}天)")
