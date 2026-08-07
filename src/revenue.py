"""
月營收資料(僅支援上市TWSE,上櫃TPEX目前查無公開端點,先回傳空值)。
資料源:證交所公開資料 t187ap05_L,MoM/YoY 百分比是證交所已經算好的欄位,直接讀取。
一次抓取全部公司,不用一檔一檔查,速度很快。
"""
import requests

REVENUE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; tw-screener/1.0)"}


def _to_float(s):
    """證交所回傳的數字有時是字串、有逗號、或空字串,安全轉成float,轉不了回傳None"""
    if s is None:
        return None
    try:
        s = str(s).replace(",", "").strip()
        if s == "" or s == "-":
            return None
        return float(s)
    except (ValueError, TypeError):
        return None


def get_monthly_revenue() -> dict:
    """
    回傳 {公司代號(str): {month, mom_pct, yoy_pct}}
    抓取失敗時回傳空字典,不會讓整個掃描程式當掉。
    """
    out = {}
    try:
        r = requests.get(REVENUE_URL, headers=HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[warn] 月營收資料抓取失敗: {e}")
        return out

    for row in data:
        code = row.get("公司代號")
        if not code:
            continue
        out[code] = {
            "month": row.get("資料年月"),
            "mom_pct": _to_float(row.get("營業收入_上月比較增減")),
            "yoy_pct": _to_float(row.get("營業收入_去年同月增減")),
        }
    return out


if __name__ == "__main__":
    import requests as _requests
    print("===== 原始API回應檢查 =====")
    try:
        raw = _requests.get(REVENUE_URL, headers=HEADERS, timeout=30).json()
        print(f"回傳筆數: {len(raw)}")
        if raw:
            print(f"\n第一筆資料的所有欄位名稱與值:")
            for k, v in raw[0].items():
                print(f"  {k!r}: {v!r}")
    except Exception as e:
        print(f"❌ API請求失敗: {e}")

    print("\n\n===== 用目前程式邏輯解析後的結果 =====")
    rev = get_monthly_revenue()
    print(f"共取得 {len(rev)} 家公司月營收資料")
    for code in list(rev)[:5]:
        print(code, rev[code])
