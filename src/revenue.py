"""
月營收資料(僅支援上市TWSE,上櫃TPEX目前查無公開端點,先回傳空值)。
資料源:證交所公開資料 t187ap05_L,MoM/YoY 百分比是證交所已經算好的欄位,直接讀取。
一次抓取全部公司,不用一檔一檔查,速度很快。
"""
import requests

REVENUE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

# 正確欄位名稱(用連字號「-」,不是底線,且百分比欄位帶「(%)」)
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


def get_monthly_revenue() -> dict:
    """回傳 {公司代號(str): {month, mom_pct, yoy_pct}},失敗回傳空字典"""
    out = {}
    try:
        r = requests.get(REVENUE_URL, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            print(f"[warn] 月營收API回傳狀態碼異常: {r.status_code}")
            print(f"[warn] 回應內容前300字: {r.text[:300]!r}")
            return out
        data = r.json()
    except Exception as e:
        print(f"[warn] 月營收資料抓取失敗: {e}")
        try:
            print(f"[warn] 狀態碼: {r.status_code}, 回應內容前300字: {r.text[:300]!r}")
        except Exception:
            pass
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


if __name__ == "__main__":
    print("===== 原始API回應檢查 =====")
    try:
        resp = requests.get(REVENUE_URL, headers=HEADERS, timeout=30)
        print(f"HTTP狀態碼: {resp.status_code}")
        print(f"Content-Type: {resp.headers.get('Content-Type')}")
        print(f"回應內容前500字:\n{resp.text[:500]}")
        if resp.status_code == 200:
            raw = resp.json()
            print(f"\n回傳筆數: {len(raw)}")
            if raw:
                print(f"\n第一筆資料的所有欄位名稱與值:")
                for k, v in raw[0].items():
                    print(f"  {k!r}: {v!r}")
    except Exception as e:
        print(f"❌ 請求過程發生例外: {e}")

    print("\n\n===== 用目前程式邏輯解析後的結果 =====")
    rev = get_monthly_revenue()
    print(f"共取得 {len(rev)} 家公司月營收資料")
    for code in list(rev)[:5]:
        print(code, rev[code])
