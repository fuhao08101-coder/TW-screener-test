"""
短線王(v11)篩選器:適合短線/權證操作的進場訊號。

篩選條件(當天同時符合,收盤價直接進場):
  ATR14絕對值 >= 8
  收盤 > 15MA
  盤中最高價 > 近3個交易日最高點(用最高價判斷突破,不用等收盤確認)
  外資+融資近9個交易日內同天雙買過,且雙買之後沒有出現過同天雙減
    (上市TWSE + 上櫃TPEX 都支援,已實測確認可正確抓取雙方資料)

這個掃描器只負責「找出符合進場條件的候選股」,不模擬停損停利
(停損停利是進場後的部位管理邏輯,回測已經驗證過,正式網頁只顯示訊號本身)。

還原日K:使用 yfinance auto_adjust=True。批次下載機制與 screener.py 相同。
"""
from __future__ import annotations
import time
import pandas as pd
import yfinance as yf

from institutional_flow import build_dual_buy_qualified_set

ATR_PERIOD = 14
ATR_MIN_THRESHOLD = 8.0
SHORT_MA_PERIOD = 15
BREAKOUT_LOOKBACK_DAYS = 3   # 突破近幾日高點(改成盤中最高價比較,不是收盤)
REQUIRE_DUAL_BUY = True  # 外資融資雙買濾網,上市櫃都已支援

HISTORY_PERIOD = "1y"
BATCH_SIZE = 150
BATCH_SLEEP = 1.0


def _calc_atr(df: pd.DataFrame, period: int) -> pd.Series:
    high = df["High"]
    low = df["Low"]
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        (high - low), (high - prev_close).abs(), (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def _evaluate_from_df(df: pd.DataFrame, ticker: str, name: str) -> dict | None:
    min_len = max(SHORT_MA_PERIOD, ATR_PERIOD, BREAKOUT_LOOKBACK_DAYS) + 20
    if df is None or df.empty or len(df) < min_len:
        return None

    close = df["Close"].dropna()
    high = df["High"]
    if len(close) < min_len:
        return None

    ma15 = close.rolling(SHORT_MA_PERIOD).mean()
    atr = _calc_atr(df, ATR_PERIOD)
    recent_high = high.rolling(BREAKOUT_LOOKBACK_DAYS).max().shift(1)

    latest_close = close.iloc[-1]
    latest_high = high.iloc[-1]
    latest_ma15 = ma15.iloc[-1]
    latest_atr = atr.iloc[-1]
    latest_recent_high = recent_high.iloc[-1]

    if pd.isna(latest_ma15) or latest_close <= latest_ma15:
        return None
    if pd.isna(latest_recent_high) or latest_high <= latest_recent_high:
        return None
    if pd.isna(latest_atr) or latest_atr < ATR_MIN_THRESHOLD:
        return None

    return {
        "ticker": ticker,
        "name": name,
        "close": round(float(latest_close), 2),
        "high": round(float(latest_high), 2),
        "ma15": round(float(latest_ma15), 2),
        "recent_high": round(float(latest_recent_high), 2),
        "atr14": round(float(latest_atr), 2),
        "signal_low": round(float(df["Low"].iloc[-1]), 2),
        "as_of": close.index[-1].strftime("%Y-%m-%d"),
    }


def _fetch_batch(tickers: list[str]) -> dict[str, pd.DataFrame]:
    out = {}
    try:
        data = yf.download(
            tickers=tickers, period=HISTORY_PERIOD, auto_adjust=True,
            group_by="ticker", threads=True, progress=False,
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


def scan_universe(universe: list[dict], progress: bool = True) -> list[dict]:
    results = []
    total = len(universe)
    ticker_to_name = {row["ticker"]: row for row in universe}

    dual_buy_qualified: set[str] = set()
    if REQUIRE_DUAL_BUY:
        dual_buy_qualified = build_dual_buy_qualified_set()

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

            if REQUIRE_DUAL_BUY:
                # 上市櫃都已支援,不再排除上櫃股票
                code = t.replace(".TWO", "").replace(".TW", "")
                if code not in dual_buy_qualified:
                    continue

            df = batch_data.get(t)
            try:
                hit = _evaluate_from_df(df, t, row["name"])
                if hit:
                    hit["market"] = row["market"]
                    results.append(hit)
            except Exception as e:
                print(f"[warn] {t} 判斷失敗: {e}")
        time.sleep(BATCH_SLEEP)

    results.sort(key=lambda r: r["atr14"], reverse=True)
    return results
