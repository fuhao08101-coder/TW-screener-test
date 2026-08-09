"""
檢查個股「近3個月內」有沒有鉅額交易紀錄(目前只支援上市TWSE,上櫃TPEx暫不支援)。
資料源:證交所「個股單一證券鉅額交易日成交資訊」,一次查詢會回傳整年度資料,
自己在程式裡篩選是否有落在最近3個月內的紀錄即可。
"""
import time
import requests
from datetime import date, datetime, timedelta

URL = "https://www.twse.com.tw/rwd/zh/block/BFIAUU_sd"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

LOOKBACK_DAYS = 90  # 「三個月內」用90天當近似值
REQUEST_SLEEP = 0.5  # 每檔股票間的延遲


def has_block_trade_recently(stock_code: str, lookback_days: int = LOOKBACK_DAYS) -> bool | None:
    """
    回傳 True(近期有鉅額交易) / False(近期沒有) / None(查詢失敗,無法判斷)
    """
    today = date.today()
    params = {
        "response": "json",
        "startDate": today.strftime("%Y%m01"),
        "endDate": today.strftime("%Y%m%d"),
        "stockNo": stock_code,
    }
    try:
        r = requests.get(URL, headers=HEADERS, params=params, timeout=20)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:
        return None

    rows = data.get("data", [])
    cutoff = today - timedelta(days=lookback_days)

    for row in rows:
        if not row or row[0] == "總計":
            continue
        date_str = row[0]  # 格式類似 "2026/08/07"
        try:
            d = datetime.strptime(date_str, "%Y/%m/%d").date()
        except (ValueError, IndexError):
            continue
        if d >= cutoff:
            return True

    return False


def check_batch(stock_codes: list[str]) -> dict[str, bool | None]:
    """依序查詢一批股票代號,回傳 {代號: True/False/None}"""
    out = {}
    for code in stock_codes:
        out[code] = has_block_trade_recently(code)
        time.sleep(REQUEST_SLEEP)
    return out
