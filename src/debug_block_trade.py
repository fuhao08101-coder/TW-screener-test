"""
探測工具:找出「鉅額交易日成交資訊」正確的API路徑與參數格式。
不是正式功能,先看資料源長什麼樣,再決定怎麼寫。
測試股票用 2330(台積電),因為它幾乎每天都有鉅額交易紀錄,好驗證。
"""
import requests
from datetime import date

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

TEST_STOCK = "2330"
TODAY_STR = date.today().strftime("%Y%m%d")
TODAY_MONTH_STR = date.today().strftime("%Y%m") + "01"

CANDIDATE_URLS = [
    f"https://www.twse.com.tw/rwd/zh/block/BFIAUU?response=json&date={TODAY_STR}&stockNo={TEST_STOCK}",
    f"https://www.twse.com.tw/rwd/zh/block/BFIAUU_sd?response=json&startDate={TODAY_MONTH_STR}&endDate={TODAY_STR}&stockNo={TEST_STOCK}",
    f"https://www.twse.com.tw/exchangeReport/BFIAUU?response=json&date={TODAY_STR}&stockNo={TEST_STOCK}",
    f"https://www.twse.com.tw/block/BFIAUU?response=json&date={TODAY_STR}&stockNo={TEST_STOCK}",
    f"https://www.twse.com.tw/rwd/zh/afterTrading/BFIAUU?response=json&date={TODAY_STR}&stockNo={TEST_STOCK}",
]

if __name__ == "__main__":
    print(f"測試股票: {TEST_STOCK}, 今天日期: {TODAY_STR}\n")
    for url in CANDIDATE_URLS:
        print(f"===== 嘗試: {url} =====")
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            print(f"HTTP狀態碼: {r.status_code}")
            print(f"Content-Type: {r.headers.get('Content-Type')}")
            if r.status_code == 200:
                try:
                    data = r.json()
                    print(f"回應內容(JSON): {data}")
                except Exception:
                    print(f"回應內容(非JSON,前300字): {r.text[:300]}")
            else:
                print(f"回應內容前200字: {r.text[:200]}")
        except Exception as e:
            print(f"❌ 請求例外: {e}")
        print()
