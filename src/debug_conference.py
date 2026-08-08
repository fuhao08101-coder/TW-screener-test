"""
探測工具:抓「上市公司每日重大訊息」(t187ap04_L),
篩出主旨含「法人說明會」的公告,印出原始欄位長相,
用來確認正式功能該怎麼解析日期。
不是正式功能,只是先看資料、再決定怎麼寫。
"""
import requests

URL = "https://openapi.twse.com.tw/v1/opendata/t187ap04_L"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

if __name__ == "__main__":
    print("===== 抓取上市公司每日重大訊息 =====")
    try:
        r = requests.get(URL, headers=HEADERS, timeout=30)
        print(f"HTTP狀態碼: {r.status_code}")
        if r.status_code != 200:
            print(f"回應內容前300字: {r.text[:300]}")
        else:
            data = r.json()
            print(f"總筆數: {len(data)}")

            if data:
                print(f"\n第一筆完整欄位(看有哪些欄位可以用):")
                for k, v in data[0].items():
                    print(f"  {k!r}: {v!r}")

            print(f"\n\n===== 篩選主旨含「法人說明會」的公告 =====")
            matched = [row for row in data if "法人說明會" in str(row.get("主旨", ""))]
            print(f"符合筆數: {len(matched)}")
            for row in matched[:5]:
                print("\n---")
                for k, v in row.items():
                    print(f"  {k!r}: {v!r}")
    except Exception as e:
        print(f"❌ 發生例外: {e}")
