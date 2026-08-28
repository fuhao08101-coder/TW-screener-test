"""
探測工具:找出櫃買中心(TPEX)權證基本資料的正確API路徑。
不是正式功能,先看資料源長什麼樣,再決定怎麼寫進正式功能。

背景:t187ap37_L(證交所)只收錄「標的是上市股票」的權證,標的是上櫃股票
(例如7751竑騰、8358金居)的權證完全不在這份資料裡,需要另外找櫃買中心
自己的對應資料。

猜測依據:之前處理月營收時發現,櫃買中心常常沿用「跟證交所一樣的代碼數字,
結尾從_L(上市)換成_O(上櫃)」這個規律(例如t187ap05_L對應t187ap05_O),
這次先試同樣規律的猜測,加上幾個備用候選。
"""
import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

CANDIDATES = [
    "https://www.tpex.org.tw/openapi/v1/tpex_mopsfin_t187ap37_O",
    "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap37_O",
    "https://www.tpex.org.tw/openapi/v1/t187ap37_O",
    "https://www.tpex.org.tw/openapi/v1/tpex_warrant_summary",
]

if __name__ == "__main__":
    for url in CANDIDATES:
        print(f"\n===== 嘗試: {url} =====")
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            print(f"HTTP狀態碼: {r.status_code}")
            print(f"Content-Type: {r.headers.get('Content-Type')}")
            if r.status_code == 200:
                text = r.text[:500]
                print(f"回應內容前500字:\n{text}")
            else:
                print(f"回應內容前200字: {r.text[:200]}")
        except Exception as e:
            print(f"❌ 請求例外: {e}")
