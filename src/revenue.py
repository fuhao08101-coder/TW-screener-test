"""
月營收資料(上市TWSE + 上櫃TPEX,兩邊欄位名稱格式相同,可共用同一套解析邏輯)。
資料源:公開資料,MoM/YoY 百分比是官方已經算好的欄位,直接讀取。
一次抓取全部公司,不用一檔一檔查,速度很快。
加上重試機制,降低外部API偶發連線失敗的影響。
"""
import time
import requests

TWSE_REVENUE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
TPEX_REVENUE_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

FIELD_CODE = "公司代號"
FIELD_MONTH = "資料年月"
FIELD_INDUSTRY = "產業別"
FIELD_MOM = "營業收入-上月比較增減(%)"
FIELD_YOY = "營業收入-去年同月增減(%)"

MAX_RETRIES = 3
RETRY_DELAY = 3  # 秒


def _to_float(s):
    if s is None:
        return None
    try:
        s = str(s).replace(",", "").strip()
        if s == "" or s == "-":
            return None
        return float(s)
    except (ValueError, TypeError):
        return None


def _fetch_one(url: str) -> dict:
    """抓單一市場的月營收,失敗會重試幾次,最終還是失敗回傳空字典(不會讓整個掃描程式當掉)"""
    out = {}
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code != 200:
                last_error = f"狀態碼 {r.status_code}"
                print(f"[warn] 第{attempt}次請求月營收失敗({url}): {last_error}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                continue

            data = r.json()
            for row in data:
                code = row.get(FIELD_CODE)
                if not code:
                    continue
                out[code] = {
                    "month": row.get(FIELD_MONTH),
                    "industry": row.get(FIELD_INDUSTRY),
                    "mom_pct": _to_float(row.get(FIELD_MOM)),
                    "yoy_pct": _to_float(row.get(FIELD_YOY)),
                }
            return out  # 成功就直接回傳,不用再重試

        except Exception as e:
            last_error = str(e)
            print(f"[warn] 第{attempt}次請求月營收失敗({url}): {last_error}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)

    print(f"[warn] 月營收抓取重試{MAX_RETRIES}次後仍失敗({url}): {last_error}")
    return out


def get_monthly_revenue() -> dict:
    """回傳 {公司代號(str): {month, mom_pct, yoy_pct}},上市+上櫃合併在一起"""
    twse_data = _fetch_one(TWSE_REVENUE_URL)
    tpex_data = _fetch_one(TPEX_REVENUE_URL)
    merged = {}
    merged.update(twse_data)
    merged.update(tpex_data)
    return merged


if __name__ == "__main__":
    rev = get_monthly_revenue()
    print(f"共取得 {len(rev)} 家公司月營收資料(上市+上櫃)")
    for code in list(rev)[:5]:
        print(code, rev[code])
