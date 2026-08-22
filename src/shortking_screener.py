"""
短線王(組合C + 大盤環境濾網)篩選器:適合短線/權證操作的進場訊號。

篩選條件(當天同時符合,收盤價直接進場):
  ATR14絕對值 >= 8
  收盤 > 15MA
  15MA乖離 >= 8%
  收盤 > 近5個交易日最高點(用收盤價確認突破,不是盤中)
  大盤環境濾網(新增,已回測驗證有效):
    上市(TWSE)股票要進場,加權指數(^TWII)當天收盤必須站上指數自己的15MA
    上櫃(TPEX)股票要進場,櫃買指數(^TWOII)當天收盤必須站上指數自己的15MA
    兩個指數各自獨立判斷。回測顯示加了這條濾網後,上市期望值+1.38%→+1.64%、
    上櫃+1.18%→+1.30%,兩邊都有改善,故正式採用。

【已拿掉外資融資雙買濾網】經全市場1年期回測驗證,加了雙買濾網後期望值反而下降,
證實是扣分項,故移除,回到純價格邏輯。

排序:依15MA乖離率由大到小排序。

還原日K:使用 yfinance auto_adjust=True。批次下載機制與 screener.py 相同。
"""
from __future__ import annotations
import time
from datetime import date, timedelta
import pandas as pd
import yfinance as yf

ATR_PERIOD = 14
ATR_MIN_THRESHOLD = 8.0
SHORT_MA_PERIOD = 15
BIAS_MIN_THRESHOLD = 8.0
BREAKOUT_LOOKBACK_DAYS = 5

TWSE_INDEX_TICKER = "^TWII"    # 加權指數
OTC_INDEX_TICKER = "^TWOII"    # 櫃買指數
INDEX_HISTORY_DAYS = 90        # 即時掃描只需要近況,抓3個月夠算15MA,比回測的5年快很多

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


def fetch_market_regime() -> dict:
    """
    回傳今天的大盤環境狀態:
    {
      "twse": {"is_strong": bool, "close": float, "ma15": float},
      "otc":  {"is_strong": bool, "close": float, "ma15": float},
    }
    抓取失敗的那一邊會回傳 None,呼叫端要自己判斷怎麼處理(目前策略是:
    抓不到就保守放行,不因為資料源問題誤擋掉整個市場)。
    """
    end_date = date.today()
    start_date = end_date - timedelta(days=INDEX_HISTORY_DAYS)

    def _fetch_one(ticker: str, max_retries: int = 3):
        df = None
        for attempt in range(1, max_retries + 1):
            try:
                df = yf.Ticker(ticker).history(start=start_date, end=end_date, auto_adjust=True)
            except Exception as e:
                print(f"[warn] 指數{ticker}第{attempt}次抓取失敗: {e}")
                df = None
            if df is not None and not df.empty and len(df) >= SHORT_MA_PERIOD + 5:
                break
            if attempt < max_retries:
                time.sleep(3)
        if df is None or df.empty or len(df) < SHORT_MA_PERIOD + 5:
            print(f"[warn] 指數{ticker}資料不足,大盤濾網這次對此市場停用(保守放行)")
            return None
        close = df["Close"].dropna()
        ma15 = close.rolling(SHORT_MA_PERIOD).mean()
        latest_close = float(close.iloc[-1])
        latest_ma15 = float(ma15.iloc[-1])
        return {
            "is_strong": latest_close > latest_ma15,
            "close": round(latest_close, 2),
            "ma15": round(latest_ma15, 2),
        }

    print("抓取大盤環境資料(加權+櫃買指數)...")
    twse_regime = _fetch_one(TWSE_INDEX_TICKER)
    otc_regime = _fetch_one(OTC_INDEX_TICKER)

    if twse_regime:
        print(f"加權指數: 收盤{twse_regime['close']} / 15MA{twse_regime['ma15']} "
              f"→ {'強(站上15MA)' if twse_regime['is_strong'] else '弱(跌破15MA)'}")
    if otc_regime:
        print(f"櫃買指數: 收盤{otc_regime['close']} / 15MA{otc_regime['ma15']} "
              f"→ {'強(站上15MA)' if otc_regime['is_strong'] else '弱(跌破15MA)'}")

    return {"twse": twse_regime, "otc": otc_regime}


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


def scan_universe(universe: list[dict], progress: bool = True) -> tuple[list[dict], dict]:
    """回傳 (results, market_regime)"""
    market_regime = fetch_market_regime()

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

            # 大盤環境濾網:抓不到資料時保守放行,不因資料源問題誤擋整個市場
            market = row["market"]
            regime = market_regime["twse"] if market == "TWSE" else market_regime["otc"]
            if regime is not None and not regime["is_strong"]:
                continue

            df = batch_data.get(t)
            try:
                hit = _evaluate_from_df(df, t, row["name"])
                if hit:
                    hit["market"] = market
                    results.append(hit)
            except Exception as e:
                print(f"[warn] {t} 判斷失敗: {e}")
        time.sleep(BATCH_SLEEP)

    results.sort(key=lambda r: r["bias_pct"], reverse=True)
    return results, market_regime
