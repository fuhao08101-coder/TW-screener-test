"""
短線王(組合C,已驗證版本)篩選器:適合短線/權證操作的進場訊號。

篩選條件(當天同時符合,收盤價直接進場):
  ATR14絕對值 >= 8
  收盤 > 15MA
  15MA乖離 >= 8%
  收盤 > 近5個交易日最高點(用收盤價確認突破,不是盤中——經回測驗證,收盤確認版本
    期望值優於盤中版本,推測是能過濾掉「當天觸價但收盤又拉回」的假突破雜訊)

【已拿掉外資融資雙買濾網】經全市場1年期回測驗證,加了雙買濾網後期望值反而從
+2.37%掉到+1.32%,勝率也下降,證實這層濾網是扣分項,不是加分項,故移除,
回到純價格邏輯(組合C)。

排序:依15MA乖離率由大到小排序。

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
SHORT_MA_PERIOD = 15
BIAS_MIN_THRESHOLD = 8.0     # 15MA乖離門檻(%)
BREAKOUT_LOOKBACK_DAYS = 5   # 突破近幾日高點(收盤價比較)

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
    bias = (close - ma15) / ma15 * 100.0
    atr = _calc_atr(df, ATR_PERIOD)
    recent_high = high.rolling(BREAKOUT_LOOKBACK_DAYS).max().shift(1)

    latest_close = close.iloc[-1]
    latest_high = high.iloc[-1]
    latest_ma15 = ma15.iloc[-1]
    latest_bias = bias.iloc[-1]
    latest_atr = atr.iloc[-1]
    latest_recent_high = recent_high.iloc[-1]

    if pd.isna(latest_ma15) or latest_close <= latest_ma15:
        return None
    if pd.isna(latest_bias) or latest_bias < BIAS_MIN_THRESHOLD:
        return None
    if pd.isna(latest_atr) or latest_atr < ATR_MIN_THRESHOLD:
        return None
    if pd.isna(latest_recent_high) or latest_close <= latest_recent_high:
        return None

    return {
        "ticker": ticker,
        "name": name,
        "close": round(float(latest_close), 2),
        "high": round(float(latest_high), 2),
        "ma15": round(float(latest_ma15), 2),
        "bias_pct": round(float(latest_bias), 2),
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
