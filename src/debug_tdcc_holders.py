"""
探測工具:確認集保結算所「集保戶股權分散表」API的真實回傳格式。
不是正式功能,先看資料源長什麼樣,再決定怎麼寫進正式的大戶濾網。

端點:https://openapi.tdcc.com.tw/v1/opendata/1-5
根據官方API文件,欄位應該是:資料日期、證券代號、持股分級、人數、股數、占集保庫存數比例%
但「持股分級」這個欄位實際的文字內容還沒看過,這次先抓一次,只挑2330(台積電)的資料印出來看。
"""
import requests
import json

URL = "https://openapi.tdcc.com.tw/v1/opendata/1-5"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}
TEST_STOCK = "2330"

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
            print(f"\n總筆數(全市場全級距合計): {len(data)}")

            if isinstance(data, list) and data:
                print(f"\n第一筆資料範例(看欄位長怎樣): {json.dumps(data[0], ensure_ascii=False, indent=2)}")

                # 篩出2330的資料,印出全部級距,才能看到「持股分級」欄位的完整文字內容
                stock_2330 = [row for row in data if row.get("證券代號") == TEST_STOCK]
                print(f"\n{TEST_STOCK}(台積電)共有 {len(stock_2330)} 個級距,完整列出:")
                for row in stock_2330:
                    print(f"  {json.dumps(row, ensure_ascii=False)}")

                # 額外印出「資料日期」的所有不重複值,確認這次抓到的是哪一週的資料、
                # 是不是只有最新一週,還是包含歷史多週資料
                all_dates = sorted(set(row.get("資料日期") for row in data))
                print(f"\n資料裡出現的「資料日期」有幾種: {len(all_dates)}")
                print(f"最早: {all_dates[0] if all_dates else 'N/A'}")
                print(f"最新: {all_dates[-1] if all_dates else 'N/A'}")
                if len(all_dates) <= 10:
                    print(f"全部日期: {all_dates}")
    except Exception as e:
        print(f"❌ 請求例外: {e}")
