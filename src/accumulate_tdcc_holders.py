"""
每週累積集保結算所「大戶持股分級」資料,為未來的「大戶籌碼增加」濾網打底。

背景:集保API(openapi.tdcc.com.tw)只給「當下最新一週」的快照,沒有歷史資料,
沒辦法回測。要做到「近兩週大戶有沒有增加」這種判斷,必須自己每週固定抓一次、
自己存起來,累積幾個月後才有足夠的歷史可以用。這支程式就是做這件事,現在先
默默跑著累積資料,不影響任何現有正式功能,之後想啟用大戶濾網時,資料已經備好。

級距代碼對照(重要:這是台股業界通用的標準15級距分類,不是官方逐字文件確認過的,
是合理推斷,如果之後發現數字兜不起來,要回頭校正這份對照表):
  1: 1-999股         2: 1,000-5,000股      3: 5,001-10,000股
  4: 10,001-15,000股 5: 15,001-20,000股    6: 20,001-30,000股
  7: 30,001-40,000股 8: 40,001-50,000股    9: 50,001-100,000股
  10: 100,001-200,000股  11: 200,001-400,000股
  12: 400,001-600,000股(超過400張的起點)
  13: 600,001-800,000股
  14: 800,001-1,000,000股(超過800張的起點)
  15: 1,000,001股以上
  16: 合計(不是一個級距,是加總列,不列入分級加總)

為了保險,不是只存「400張以上加總」這種算好的結果,是存「每個級距各自的股數」,
未來如果對照表需要校正,原始資料還在,可以重新計算,不用重新累積。

輸出:docs/tdcc_holders_history.json,每次執行append一筆這週的快照(不會覆蓋舊資料)。
"""
import json
import os
import time
from datetime import datetime, timezone, timedelta
import requests

URL = "https://openapi.tdcc.com.tw/v1/opendata/1-5"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "tdcc_holders_history.json")

# 400張以上、800張以上分別對應哪些級距代碼(見檔案開頭說明)
TIERS_400_UP = {"12", "13", "14", "15"}
TIERS_800_UP = {"14", "15"}


def _normalize_code(raw_code: str) -> str:
    """集保回傳的證券代號可能有補零(例如台積電可能是002330而不是2330),去掉開頭多餘的0"""
    code = raw_code.strip()
    stripped = code.lstrip("0")
    return stripped if stripped else code


def fetch_and_process() -> dict | None:
    print(f"請求網址: {URL}")
    try:
        r = requests.get(URL, headers=HEADERS, timeout=90)
    except Exception as e:
        print(f"[warn] 請求例外: {e}")
        return None

    if r.status_code != 200:
        print(f"[warn] HTTP狀態碼異常: {r.status_code}")
        return None

    try:
        data = r.json()
    except Exception as e:
        print(f"[warn] 回應不是合法JSON: {e}")
        return None

    print(f"總筆數(全市場全級距合計): {len(data)}")

    # 依股票代號分組,累加每個級距的股數
    per_stock = {}  # code -> {tier_code: shares}
    data_date = None

    for row in data:
        raw_code = row.get("證券代號", "")
        code = _normalize_code(raw_code)
        tier = str(row.get("持股分級", "")).strip()
        try:
            shares = int(row.get("股數", "0"))
        except (ValueError, TypeError):
            shares = 0

        # 取得資料日期(欄位名稱可能有BOM字元,寬鬆比對)
        if data_date is None:
            for k, v in row.items():
                if "資料日期" in k:
                    data_date = v
                    break

        if code not in per_stock:
            per_stock[code] = {}
        per_stock[code][tier] = per_stock[code].get(tier, 0) + shares

    print(f"共 {len(per_stock)} 檔證券(含股票以外的其他證券,例如債券ETF)")

    # 只保留看起來像股票代號的(4碼數字),並且計算400張以上、800張以上的加總跟占比
    summary = {}
    for code, tiers in per_stock.items():
        if not (code.isdigit() and len(code) == 4):
            continue
        total_shares = sum(tiers.values())
        if total_shares <= 0:
            continue
        shares_400_up = sum(v for k, v in tiers.items() if k in TIERS_400_UP)
        shares_800_up = sum(v for k, v in tiers.items() if k in TIERS_800_UP)
        summary[code] = {
            "total_shares": total_shares,
            "shares_400_up": shares_400_up,
            "shares_800_up": shares_800_up,
            "pct_400_up": round(shares_400_up / total_shares * 100, 3) if total_shares else 0,
            "pct_800_up": round(shares_800_up / total_shares * 100, 3) if total_shares else 0,
            "raw_tiers": tiers,  # 保留原始級距分布,未來對照表校正用得到
        }

    print(f"篩選出 {len(summary)} 檔4碼股票代號的股票")

    return {
        "data_date": data_date,
        "fetched_at": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
        "stocks": summary,
    }


def load_history() -> list[dict]:
    if not os.path.exists(OUTPUT_PATH):
        return []
    try:
        with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def main():
    snapshot = fetch_and_process()
    if snapshot is None:
        print("❌ 這次抓取失敗,不寫入任何資料,保留之前累積的歷史")
        return

    if not snapshot["stocks"]:
        print("❌ 這次解析出的股票數量是0,判斷資料異常,不寫入,保留之前累積的歷史")
        return

    history = load_history()

    # 如果這週的資料日期跟已經存過的一樣,就不重複存(避免同一週資料被存好幾次)
    existing_dates = {h.get("data_date") for h in history}
    if snapshot["data_date"] in existing_dates:
        print(f"這週的資料({snapshot['data_date']})已經存過了,不重複累積")
        return

    history.append(snapshot)
    history.sort(key=lambda h: h.get("data_date") or "")

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

    print(f"✅ 已累積新一週資料(資料日期:{snapshot['data_date']}),"
          f"目前總共累積 {len(history)} 週的歷史快照")


if __name__ == "__main__":
    main()
