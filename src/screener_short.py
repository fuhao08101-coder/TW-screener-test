"""
放空篩選核心邏輯:乖離背離訊號(原始版)

  條件1:近 REF_LOOKBACK_DAYS 個交易日內,曾出現「收盤價相對15MA乖離率 >= REF_BIAS_THRESHOLD%」
        的那一天,當作「最高乖離參考日」
  條件2:今天「盤中最高價」超過「參考日的收盤價」(用盤中價比對,不是收盤價——
        代表股價創了新高,但可能只是盤中衝高、收盤未必真的過前高)
  條件3:今天的乖離率 比 參考日的乖離率 小(背離,動能減弱訊號)
  條件4:今天 ATR14「絕對值」>= ATR_MIN_THRESHOLD

還原日K:使用 yfinance auto_adjust=True,會依除權息回推調整 OHLC。

架構比照 screener.py:批次下載、逐檔套用篩選邏輯、回傳結果清單。
"""
from __future__ import annotations
import time
import re
from datetime import date, datetime
import pandas as pd
import yfinance as yf
import requests

# ------- 可調參數 -------
BIAS_MA_PERIOD = 15          # 15MA
REF_LOOKBACK_DAYS = 40       # 往前找參考日的範圍(交易日)——放寬到40天,涵蓋較長的背離間隔
REF_BIAS_THRESHOLD = 20.0    # 參考日的乖離率門檻(%)

ATR_PERIOD = 14
ATR_MIN_THRESHOLD = 10.0     # 訊號日ATR14「絕對值」門檻

REQUIRE_PUT_WARRANT = False  # 認售權證檢查(TWSE API不穩定,關閉,改手動判斷)

REVENUE_URL_TWSE = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
REVENUE_URL_TPEX = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"
REVENUE_MAX_RETRIES = 3
REVENUE_RETRY_DELAY = 3  # 秒

HISTORY_PERIOD = "2y"        # 抓多久的歷史資料(要夠算lookback+15MA+ATR14)
BATCH_SIZE = 150
BATCH_SLEEP = 1.0

WARRANT_INFO_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap37_L"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}
# ------------------------


def _parse_twse_date(s: str) -> date | None:
    """嘗試解析TWSE常見的日期格式(西元年或民國年),失敗回傳None"""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    # 民國年格式,例如 115/08/12
    m = re.match(r"^(\d{2,3})/(\d{1,2})/(\d{1,2})$", s)
    if m:
        try:
            return date(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def fetch_put_warrant_underlyings() -> set[str]:
    """
    抓「上市權證基本資料」,回傳目前還沒到期、類型為「認售」的權證,
    其標的股票代號集合(只查一次,不是逐檔股票查,避免速度太慢)。
    查詢失敗回傳空集合(此時 REQUIRE_PUT_WARRANT 篩選會讓所有股票被濾掉,
    需要留意 log 裡的警告訊息)。
    """
    try:
        r = requests.get(WARRANT_INFO_URL, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            print(f"[warn] 認售權證清單查詢失敗,status={r.status_code}")
            return set()
        data = r.json()
    except Exception as e:
        print(f"[warn] 認售權證清單查詢失敗: {e}")
        return set()

    today = date.today()
    underlyings: set[str] = set()
    for row in data:
        try:
            wtype = row.get("權證類型", "") or ""
            if "認售" not in wtype:
                continue
            last_day = _parse_twse_date(row.get("最後交易日", ""))
            if last_day is not None and last_day < today:
                continue  # 已經到期下市的權證不算數

            underlying_raw = (row.get("標的證券/指數", "") or "").strip()
            m = re.match(r"(\d{4,6})", underlying_raw)
            if m:
                underlyings.add(m.group(1))
        except Exception:
            continue

    print(f"目前有效認售權證涵蓋 {len(underlyings)} 檔標的股票")
    return underlyings


def _to_float(s):
    if s is None:
        return None
    try:
        s = str(s).replace(",", "").strip()
        if s == "" or s == "-":
            return None
        return round(float(s), 2)
    except (ValueError, TypeError):
        return None


def _fetch_revenue_one(url: str) -> dict:
    """抓單一市場的月營收,失敗會重試幾次,最終還是失敗回傳空字典(不會讓整個掃描程式當掉)"""
    out = {}
    last_error = None
    for attempt in range(1, REVENUE_MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code != 200:
                last_error = f"狀態碼 {r.status_code}"
                print(f"[warn] 第{attempt}次請求月營收失敗({url}): {last_error}")
                if attempt < REVENUE_MAX_RETRIES:
                    time.sleep(REVENUE_RETRY_DELAY)
                continue
            data = r.json()
            for row in data:
                code = row.get("公司代號")
                if not code:
                    continue
                out[code] = {
                    "month": row.get("資料年月"),
                    "mom_pct": _to_float(row.get("營業收入-上月比較增減(%)")),
                    "yoy_pct": _to_float(row.get("營業收入-去年同月增減(%)")),
                }
            return out  # 成功就直接回傳,不用再重試
        except Exception as e:
            last_error = str(e)
            print(f"[warn] 第{attempt}次請求月營收失敗({url}): {last_error}")
            if attempt < REVENUE_MAX_RETRIES:
                time.sleep(REVENUE_RETRY_DELAY)
    print(f"[warn] 月營收抓取重試{REVENUE_MAX_RETRIES}次後仍失敗({url}): {last_error}")
    return out


def fetch_revenue_map() -> dict[str, dict]:
    """回傳 {公司代號: {month, mom_pct, yoy_pct}},上市+上櫃合併在一起"""
    twse_data = _fetch_revenue_one(REVENUE_URL_TWSE)
    tpex_data = _fetch_revenue_one(REVENUE_URL_TPEX)
    merged = {}
    merged.update(twse_data)
    merged.update(tpex_data)
    print(f"營收資料共取得 {len(merged)} 家公司(上市+上櫃)")
    return merged



def _calc_atr(df: pd.DataFrame, period: int) -> pd.Series:
    high = df["High"]
    low = df["Low"]
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def _fetch_batch(tickers: list[str]) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    try:
        data = yf.download(
            tickers=tickers,
            period=HISTORY_PERIOD,
            auto_adjust=True,
            group_by="ticker",
            threads=True,
            progress=False,
        )
    except Exception as e:
        print(f"[warn] 批次下載失敗({len(tickers)}檔): {e}")
        return out

    if data is None or data.empty:
        return out

    if len(tickers) == 1:
        out[tickers[0]] = data
        return out

    for t in tickers:
        try:
            sub = data[t]
            if sub is not None and not sub.empty:
                out[t] = sub.dropna(how="all")
        except (KeyError, Exception):
            continue

    return out


def _evaluate_from_df(df: pd.DataFrame, ticker: str, name: str) -> dict | None:
    """拿到已經抓好的單一股票歷史資料後,套用乖離背離放空篩選邏輯。只看「最新一天」是否觸發。"""
    min_len = BIAS_MA_PERIOD + REF_LOOKBACK_DAYS + 5

    if df is None or df.empty:
        return None

    close = df["Close"].dropna()
    high = df["High"]

    if len(close) < min_len:
        return None

    ma15 = close.rolling(BIAS_MA_PERIOD).mean()
    bias = (close - ma15) / ma15 * 100.0
    atr = _calc_atr(df, ATR_PERIOD)

    t = len(close) - 1  # 只看最新一天

    window_start = max(0, t - REF_LOOKBACK_DAYS)
    window_bias = bias.iloc[window_start:t]
    if window_bias.isna().all():
        return None

    ref_pos = window_bias.values.argmax()
    ref_idx = window_start + ref_pos
    ref_bias = bias.iloc[ref_idx]
    ref_close = close.iloc[ref_idx]

    if pd.isna(ref_bias) or ref_bias <= REF_BIAS_THRESHOLD:
        return None

    today_high = high.iloc[t]
    today_close = close.iloc[t]
    today_bias = bias.iloc[t]
    today_atr = atr.iloc[t]

    if pd.isna(today_bias) or pd.isna(today_atr):
        return None

    made_new_high = today_high > ref_close
    bias_shrunk = today_bias < ref_bias
    atr_ok = today_atr >= ATR_MIN_THRESHOLD

    if not (made_new_high and bias_shrunk and atr_ok):
        return None

    return {
        "ticker": ticker,
        "name": name,
        "close": round(float(today_close), 2),
        "signal_high": round(float(today_high), 2),
        "bias_pct": round(float(today_bias), 2),
        "ref_date": close.index[ref_idx].strftime("%Y-%m-%d"),
        "ref_close": round(float(ref_close), 2),
        "ref_bias_pct": round(float(ref_bias), 2),
        "atr14": round(float(today_atr), 2),
        "as_of": close.index[-1].strftime("%Y-%m-%d"),
    }


def scan_universe_short(universe: list[dict], progress: bool = True) -> list[dict]:
    results = []
    total = len(universe)
    ticker_to_name = {row["ticker"]: row for row in universe}

    put_warrant_underlyings: set[str] = set()
    if REQUIRE_PUT_WARRANT:
        print("查詢目前有效的認售權證清單...")
        put_warrant_underlyings = fetch_put_warrant_underlyings()

    print("查詢最新月營收資料...")
    revenue_map = fetch_revenue_map()

    all_tickers = [row["ticker"] for row in universe]
    batches = [all_tickers[i:i + BATCH_SIZE] for i in range(0, len(all_tickers), BATCH_SIZE)]

    done = 0
    for batch_idx, batch in enumerate(batches, 1):
        if progress:
            print(f"批次 {batch_idx}/{len(batches)}(共 {done}/{total} 檔已處理)")

        batch_data = _fetch_batch(batch)

        for t in batch:
            done += 1
            row = ticker_to_name.get(t)
            if row is None:
                continue

            if REQUIRE_PUT_WARRANT:
                code = t.replace(".TWO", "").replace(".TW", "")
                if code not in put_warrant_underlyings:
                    continue  # 沒有認售權證可買,不納入結果

            df = batch_data.get(t)
            try:
                hit = _evaluate_from_df(df, t, row["name"])
                if hit:
                    hit["market"] = row["market"]
                    code = t.replace(".TWO", "").replace(".TW", "")
                    rev = revenue_map.get(code, {})
                    hit["revenue_month"] = rev.get("month")
                    hit["revenue_mom_pct"] = rev.get("mom_pct")
                    hit["revenue_yoy_pct"] = rev.get("yoy_pct")
                    results.append(hit)
            except Exception as e:
                print(f"[warn] {t} 判斷失敗: {e}")

        time.sleep(BATCH_SLEEP)

    results.sort(key=lambda r: r["bias_pct"])  # 乖離縮得越多(背離越明顯)排越前面
    return results
