"""
探測工具:找出櫃買中心「櫃買指數(月查詢)」官方報表的正確API路徑與參數格式。
不是正式功能,先看資料源長什麼樣,再決定怎麼寫進正式的指數歷史資料抓取邏輯。

頁面來源:https://www.tpex.org.tw/web/stock/iNdex_info/inxh/Inx.php?l=zh-tw
(這是「櫃買指數(月查詢)」的官方頁面,底下應該有一個回傳資料的API,
 這次先試幾個最可能的候選網址)
"""
import requests
from datetime import date

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

today = date.today()
roc_year = today.year - 1911
date_slash = f"{roc_year}/{today.month:02d}"

CANDIDATES = [
    {
        "name": "候選1:inx_result.php (o=data)",
        "url": "https://www.tpex.org.tw/web/stock/iNdex_info/inxh/inx_result.php",
        "params": {"l": "zh-tw", "d": date_slash, "o": "data"},
    },
    {
        "name": "候選2:inx_result.php (無o參數)",
        "url": "https://www.tpex.org.tw/web/stock/iNdex_info/inxh/inx_result.php",
        "params": {"l": "zh-tw", "d": date_slash},
    },
    {
        "name": "候選3:Inx_result.php (大寫I)",
        "url": "https://www.tpex.org.tw/web/stock/iNdex_info/inxh/Inx_result.php",
        "params": {"l": "zh-tw", "d": date_slash, "o": "data"},
    },
    {
        "name": "候選4:同目錄猜測其他檔名 inx_his.php",
        "url": "https://www.tpex.org.tw/web/stock/iNdex_info/inxh/inx_his.php",
        "params": {"l": "zh-tw", "d": date_slash, "o": "data"},
    },
]

if __name__ == "__main__":
    print(f"今天日期(民國年/月): {date_slash}\n")
    for c in CANDIDATES:
        print(f"\n===== {c['name']} =====")
        print(f"URL: {c['url']}")
        print(f"參數: {c['params']}")
        try:
            r = requests.get(c["url"], headers=HEADERS, params=c["params"], timeout=20)
            print(f"HTTP狀態碼: {r.status_code}")
            print(f"Content-Type: {r.headers.get('Content-Type')}")
            if r.status_code == 200:
                text = r.text[:500]
                print(f"回應內容前500字:\n{text}")
            else:
                print(f"回應內容前200字: {r.text[:200]}")
        except Exception as e:
            print(f"❌ 請求例外: {e}")
