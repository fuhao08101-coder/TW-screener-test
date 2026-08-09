"""
抓取台股上市(TWSE) + 上櫃(TPEx) 股票代號清單。
資料來源為交易所公開資料 API,免費、免申請。
加上重試機制,避免單次網路不穩定(常見於免費公開API)就整批清單抓空,
導致某個市場的股票整批消失於掃描範圍之外。
"""
import time
import requests

TWSE_LIST_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_LIST_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}
MAX_RETRIES = 3
RETRY_DELAY = 3  # 秒


def _get_with_retry(url: str, max_retries: int = MAX_RETRIES):
    """帶重試機制的 GET 請求,失敗會等幾秒後再試,最多試 max_retries 次"""
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_error = e
            print(f"[warn] 第{attempt}次請求失敗({url}): {e}")
            if attempt < max_retries:
                time.sleep(RETRY_DELAY)
    raise last_error


def get_twse_list() -> list[dict]:
    """上市公司清單,回傳 [{code, name, market}]"""
    data = _get_with_retry(TWSE_LIST_URL)
    out = []
    for row in data:
        code = row.get("公司代號")
        name = row.get("公司簡稱")
        if code and code.isdigit():
            out.append({"code": code, "name": name, "market": "TWSE"})
    return out


def get_tpex_list() -> list[dict]:
    """上櫃公司清單,回傳 [{code, name, market}]"""
    data = _get_with_retry(TPEX_LIST_URL)
    out = []
    for row in data:
        code = row.get("SecuritiesCompanyCode") or row.get("Code")
        name = row.get("CompanyName") or row.get("Name")
        if code and str(code).isdigit():
            out.append({"code": str(code), "name": name, "market": "TPEX"})
    return out


def get_universe(include_otc: bool = True) -> list[dict]:
    """
    取得完整股票清單。任一來源失敗不會讓整體掛掉,只會少那個市場的股票
    (重試3次都失敗才會真的少,大幅降低偶發連線問題的影響)。
    yfinance 代號規則: 上市加 .TW, 上櫃加 .TWO
    """
    universe = []
    try:
        universe += get_twse_list()
    except Exception as e:
        print(f"[warn] 上市清單抓取失敗(重試{MAX_RETRIES}次後仍失敗): {e}")

    if include_otc:
        try:
            universe += get_tpex_list()
        except Exception as e:
            print(f"[warn] 上櫃清單抓取失敗(重試{MAX_RETRIES}次後仍失敗): {e}")

    for row in universe:
        suffix = ".TW" if row["market"] == "TWSE" else ".TWO"
        row["ticker"] = f"{row['code']}{suffix}"

    return universe


if __name__ == "__main__":
    u = get_universe()
    print(f"共取得 {len(u)} 檔股票")
    twse_count = sum(1 for r in u if r["market"] == "TWSE")
    tpex_count = sum(1 for r in u if r["market"] == "TPEX")
    print(f"上市: {twse_count} 檔, 上櫃: {tpex_count} 檔")
