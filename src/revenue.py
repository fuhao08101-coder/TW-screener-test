"""
月營收資料(上市TWSE + 上櫃TPEX,兩邊欄位名稱格式相同,可共用同一套解析邏輯)。
資料源:公開資料,MoM/YoY 百分比是官方已經算好的欄位,直接讀取。
一次抓取全部公司,不用一檔一檔查,速度很快。
"""
import requests

TWSE_REVENUE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
TPEX_REVENUE_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

FIELD_CODE = "公司代號"
FIELD_MONTH = "資料年月"
FIELD_MOM = "營業收入-上月比較增減(%)"
FIELD_YOY = "營業收入-去年同月增減(%)"


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
    """抓單一市場的月營收,失敗回傳空字典,不會讓整個掃描程式當掉"""
    out = {}
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            print(f"[warn] 月營收API回傳狀態碼異常({url}): {r.status_code}")
            return out
        data = r.json()
    except Exception as e:
        print(f"[warn] 月營收資料抓取失敗({url}): {e}")
        return out

    for row in data:
        code = row.get(FIELD_CODE)
        if not code:
            continue
        out[code] = {
            "month": row.get(FIELD_MONTH),
            "mom_pct": _to_float(row.get(FIELD_MOM)),
            "yoy_pct": _to_float(row.get(FIELD_YOY)),
        }
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
