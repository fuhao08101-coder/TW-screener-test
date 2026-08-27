"""
探測工具:確認TWSE「上市權證基本資料」API的真實回傳格式。
不是正式功能,先看資料源長什麼樣,再決定怎麼寫進正式的權證濾網。

端點:https://openapi.twse.com.tw/v1/opendata/t187ap37_L
已知欄位(來自其他開發者的測試程式碼確認過):
  出表日期、權證代號、權證簡稱、權證類型、類別、流動量提供者報價方式、
  履約開始日、最後交易日、履約截止日、發行單位數量(仟單位)、結算方式、
  標的證券/指數、最新標的履約配發數量、原始履約價格、...、備註
這次要確認的是:標的證券的股票代號,到底是單獨一個欄位,還是包在
「標的證券/指數」這個欄位的文字裡面(需要自己解析)。
"""
import requests
import json

URL = "https://openapi.twse.com.tw/v1/opendata/t187ap37_L"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

if __name__ == "__main__":
    print(f"請求網址: {URL}")
    try:
        r = requests.get(URL, headers=HEADERS, timeout=60)
        print(f"HTTP狀態碼: {r.status_code}")
        print(f"Content-Type: {r.headers.get('Content-Type')}")

        if r.status_code != 200:
            print(f"回應內容前500字: {r.text[:500]}")
        else:
            data = r.json()
            print(f"\n總筆數(全部權證): {len(data)}")

            if isinstance(data, list) and data:
                print(f"\n所有欄位名稱: {list(data[0].keys())}")
                print(f"\n第一筆完整資料: {json.dumps(data[0], ensure_ascii=False, indent=2)}")

                # 找台積電(2330)相關的權證,看標的欄位長怎樣
                tsmc_warrants = [row for row in data if "2330" in str(row.get("標的證券/指數", ""))
                                  or "台積電" in str(row.get("標的證券/指數", ""))]
                print(f"\n標的是台積電(2330)相關的權證,共 {len(tsmc_warrants)} 檔,列出前3筆:")
                for row in tsmc_warrants[:3]:
                    print(f"  {json.dumps(row, ensure_ascii=False)}")

                # 統計不重複的「標的證券/指數」欄位值,看格式規律
                unique_underlyings = set(str(row.get("標的證券/指數", "")) for row in data)
                print(f"\n不重複的「標的證券/指數」欄位值,共 {len(unique_underlyings)} 種,列出前10個範例:")
                for u in list(unique_underlyings)[:10]:
                    print(f"  {u!r}")
    except Exception as e:
        print(f"❌ 請求例外: {e}")
