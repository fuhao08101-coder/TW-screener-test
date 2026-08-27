"""
取得「目前有發行中權證的標的股票」代號集合。

資料源:TWSE官方「上市權證基本資料」t187ap37_L
  https://openapi.twse.com.tw/v1/opendata/t187ap37_L
已實測確認欄位:「標的證券/指數」存的是公司名稱(例如"AES-KY"),不是股票代號,
所以要用 universe.py 的代號↔名稱對照表,反查回股票代號。

權證的標的不一定是個股,也可能是指數(例如電子類指數),這種名稱在股票清單裡
查不到是正常現象,會被歸類到「無法比對」,不會被誤判成某檔股票的權證。

用法:
    from warrant_underlyings import get_warrant_underlying_codes
    codes = get_warrant_underlying_codes(universe)  # universe來自 get_universe()
    # codes 是一個 set,裡面是目前有發行中權證的股票代號(4碼字串)
"""
import requests

URL = "https://openapi.twse.com.tw/v1/opendata/t187ap37_L"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


def _normalize_name(name: str) -> str:
    """統一名稱格式,去除空白、統一全形半形符號,提升比對成功率"""
    if not name:
        return ""
    name = name.strip()
    name = name.replace("　", "").replace(" ", "")
    name = name.replace("－", "-").replace("─", "-")
    return name


def get_warrant_underlying_codes(universe: list[dict], max_retries: int = 3) -> tuple[set, dict]:
    """
    回傳 (合格代號集合, 診斷資訊)
    診斷資訊包含:總權證檔數、成功比對到股票的檔數、比對不到的名稱清單(通常是指數,前20個當範例)
    """
    import time

    # 建立「名稱 -> 代號」對照表(用universe.py既有的清單反查)
    name_to_code = {}
    for row in universe:
        norm_name = _normalize_name(row.get("name", ""))
        if norm_name:
            name_to_code[norm_name] = row["code"]

    data = None
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(URL, headers=HEADERS, timeout=60)
            if r.status_code == 200:
                data = r.json()
                break
        except Exception as e:
            print(f"[warn] 權證資料第{attempt}次抓取失敗: {e}")
        if attempt < max_retries:
            time.sleep(3)

    if not data:
        print("[warn] 權證資料抓取失敗,回傳空集合(此濾網這次會讓所有股票被排除,不建議在這種狀況下使用)")
        return set(), {"error": "fetch_failed"}

    matched_codes = set()
    unmatched_names = set()
    total_warrants = len(data)

    for row in data:
        underlying_raw = row.get("標的證券/指數", "")
        norm_underlying = _normalize_name(underlying_raw)
        code = name_to_code.get(norm_underlying)
        if code:
            matched_codes.add(code)
        else:
            unmatched_names.add(underlying_raw)

    diagnostics = {
        "total_warrants": total_warrants,
        "matched_stock_codes": len(matched_codes),
        "unmatched_underlying_names_sample": list(unmatched_names)[:20],
        "unmatched_count": len(unmatched_names),
    }

    print(f"權證資料:共 {total_warrants} 檔權證,比對出 {len(matched_codes)} 檔有發行權證的股票,"
          f"{len(unmatched_names)} 種標的名稱無法比對(通常是指數,不是問題)")

    return matched_codes, diagnostics


if __name__ == "__main__":
    import sys
    import os
    sys.path.insert(0, os.path.dirname(__file__))
    from universe import get_universe

    print("抓取股票清單...")
    universe = get_universe(include_otc=True)
    print(f"共 {len(universe)} 檔股票")

    codes, diag = get_warrant_underlying_codes(universe)
    print(f"\n診斷資訊: {diag}")
    print(f"\n有發行權證的股票代號範例(前20檔): {sorted(list(codes))[:20]}")
