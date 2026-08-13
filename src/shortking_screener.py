"""
短線王(v10-C)篩選器:適合短線/權證操作的進場訊號。

篩選條件(當天同時符合,收盤價直接進場):
  ATR14絕對值 >= 8
  收盤 > 15MA
  15MA乖離(收盤相對15MA) >= 13%

這個掃描器只負責「找出符合進場條件的候選股」,不模擬停損停利
(停損停利是進場後的部位管理邏輯,回測已經驗證過,正式網頁只顯示訊號本身)。

還原日K:使用 yfinance auto_adjust=True。批次下載機制與 screener.py 相同。
"""
from __future__ import annotations
import time
import pandas as pd
import yfinance as yf

ATR_PERIOD = 14
ATR_MIN_THRESHOLD = 8.0
BIAS_MA_PERIOD = 15
BIAS_MIN_THRESHOLD = 13.0

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
    min_len = max(BIAS_MA_PERIOD, ATR_PERIOD) + 20
    if df is None or df.empty or len(df) < min_len:
        return None

    close = df["Close"].dropna()
    if len(close) < min_len:
        return None

    ma15 = close.rolling(BIAS_MA_PERIOD).mean()
    bias = (close - ma15) / ma15 * 100.0
    atr = _calc_atr(df, ATR_PERIOD)

    latest_close = close.iloc[-1]
    latest_ma15 = ma15.iloc[-1]
    latest_bias = bias.iloc[-1]
    latest_atr = atr.iloc[-1]

    if pd.isna(latest_ma15) or latest_close <= latest_ma15:
        return None
    if pd.isna(latest_bias) or latest_bias < BIAS_MIN_THRESHOLD:
        return None
    if pd.isna(latest_atr) or latest_atr < ATR_MIN_THRESHOLD:
        return None

    return {
        "ticker": ticker,
        "name": name,
        "close": round(float(latest_close), 2),
        "ma15": round(float(latest_ma15), 2),
        "bias_pct": round(float(latest_bias), 2),
        "atr14": round(float(latest_atr), 2),
        "signal_low": round(float(df["Low"].iloc[-1]), 2),  # 訊號日最低點(停損參考用)
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
            df = batch_data.get(t)
            try:
                hit = _evaluate_from_df(df, t, row["name"])
                if hit:
                    hit["market"] = row["market"]
                    results.append(hit)
            except Exception as e:
                print(f"[warn] {t} 判斷失敗: {e}")
        time.sleep(BATCH_SLEEP)

    results.sort(key=lambda r: r["bias_pct"], reverse=True)
    return results
