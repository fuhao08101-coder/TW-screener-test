"""
核心篩選邏輯:
  條件1:最近 LOOKBACK_DAYS 個交易日內,曾出現「收盤價相對15MA乖離率 >= BIAS_THRESHOLD%」
  條件2:最新一根還原日K收盤價 > SMA87
  條件3:最近 MA87_BREACH_LOOKBACK 個交易日內,收盤價不得曾經跌破87MA(只看收盤價,不看盤中)
  條件4:SMA87 > SMA{SECOND_MA_PERIOD}(中期均線站上長期均線,多頭排列結構)
  條件5:ATR14「絕對值」>= ATR_MIN_THRESHOLD(14日平均真實區間,衡量波動大小)

還原日K:使用 yfinance auto_adjust=True,會依除權息回推調整 OHLC。

【效能優化】改用 yf.download() 批次下載,一次網路請求抓一整批股票(BATCH_SIZE檔)。
"""
from __future__ import annotations
import time
import pandas as pd
import yfinance as yf

# ------- 可調參數 -------
LOOKBACK_DAYS = 30          # 條件1:近幾個交易日內找乖離觸發
BIAS_MA_PERIOD = 15         # 15MA
BIAS_THRESHOLD = 20.0       # 乖離門檻(%)
LONG_MA_PERIOD = 87         # SMA87
BIAS_DIRECTION = "up"       # "up"=只抓正乖離(急漲) / "down"=只抓負乖離(急跌) / "both"=兩者都抓

MA87_BREACH_LOOKBACK = 15   # 條件3:近幾個交易日內不得跌破87MA(用收盤價判斷)

SECOND_MA_PERIOD = 284      # 條件4:第二條均線天數
REQUIRE_MA_ALIGNMENT = True # 是否啟用「SMA87 > SMA{SECOND_MA_PERIOD}」濾網

ATR_PERIOD = 14             # ATR14 計算天數
ATR_MIN_THRESHOLD = 9.0     # 條件5a:ATR14「絕對值」要 >= 這個數字,低於就剔除
ATR_MIN_PCT_THRESHOLD = 1.5 # 條件5b:ATR14「佔股價百分比」要 >= 這個數字(%),低於就剔除
REQUIRE_ATR_MIN = True      # 是否啟用「ATR14門檻」濾網(絕對值+百分比,兩個都要通過)

HISTORY_PERIOD = "2y"       # 抓多久的歷史資料
BATCH_SIZE = 150            # 批次下載:每批幾檔股票一起抓
BATCH_SLEEP = 1.0           # 每批之間的延遲(秒),避免整批請求被限流

# --- 新股票獨立規則(上市天數不夠算SMA87/SMA284的股票,走這條簡化規則) ---
NEW_LISTING_ENABLE = True   # 是否啟用新股獨立判斷
NEW_LISTING_MIN_DAYS = 45   # 新股至少要有這麼多個交易日資料,才嘗試簡化判斷(太新的還是抓不到)
# ------------------------


def _calc_atr(df: pd.DataFrame, period: int) -> pd.Series:
    """標準 ATR 計算:先算 True Range,再取 N 日移動平均(用 Wilder 平滑法接近業界慣例)"""
    high = df["High"]
    low = df["Low"]
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def _evaluate_new_listing(df: pd.DataFrame, ticker: str, name: str) -> dict | None:
    """
    新股簡化判斷:股票上市天數不夠算 SMA284,但只要湊得到 SMA87 就套用這條簡化規則:
      條件1:近 LOOKBACK_DAYS 日內乖離觸及門檻
      條件2:收盤 > SMA87
      條件3:近 MA87_BREACH_LOOKBACK 日內不得跌破 SMA87(跟老股票一樣)
    不檢查 SMA284、不檢查ATR14(資料量不足,不強求)。
    """
    close = df["Close"].dropna()
    if len(close) < LONG_MA_PERIOD + 10:  # 至少要能算出穩定的SMA87
        return None

    ma15 = close.rolling(BIAS_MA_PERIOD).mean()
    ma87 = close.rolling(LONG_MA_PERIOD).mean()
    bias = (close - ma15) / ma15 * 100.0

    lookback = min(LOOKBACK_DAYS, len(close))
    recent_bias = bias.tail(lookback)
    if recent_bias.isna().all():
        return None

    if BIAS_DIRECTION == "up":
        hit = recent_bias.max() >= BIAS_THRESHOLD
        trigger_val = recent_bias.max()
    elif BIAS_DIRECTION == "down":
        hit = recent_bias.min() <= -BIAS_THRESHOLD
        trigger_val = recent_bias.min()
    else:
        hit_up = recent_bias.max() >= BIAS_THRESHOLD
        hit_down = recent_bias.min() <= -BIAS_THRESHOLD
        hit = hit_up or hit_down
        trigger_val = recent_bias.max() if abs(recent_bias.max()) >= abs(recent_bias.min()) else recent_bias.min()

    if not hit:
        return None

    trigger_date = recent_bias.idxmax() if trigger_val > 0 else recent_bias.idxmin()

    latest_close = close.iloc[-1]
    latest_ma87 = ma87.iloc[-1]
    if pd.isna(latest_ma87) or latest_close <= latest_ma87:
        return None

    # --- 條件3:近 MA87_BREACH_LOOKBACK 日內不得跌破 SMA87(跟老股票同一套規則) ---
    breach_lookback = min(MA87_BREACH_LOOKBACK, len(close))
    recent_close_87 = close.tail(breach_lookback)
    recent_ma87_87 = ma87.tail(breach_lookback)
    if (recent_close_87 < recent_ma87_87).any():
        return None

    return {
        "ticker": ticker,
        "name": name,
        "close": round(float(latest_close), 2),
        "ma87": round(float(latest_ma87), 2),
        "ma_second": None,
        "atr14": None,
        "atr14_pct": None,
        "bias_pct": round(float(trigger_val), 2),
        "bias_date": trigger_date.strftime("%Y-%m-%d"),
        "as_of": close.index[-1].strftime("%Y-%m-%d"),
        "new_listing": True,
        "listed_days": len(close),
    }


def _evaluate_from_df(df: pd.DataFrame, ticker: str, name: str) -> dict | None:
    """拿到已經抓好的單一股票歷史資料後,套用篩選邏輯。不做任何網路請求。"""
    min_len = max(LONG_MA_PERIOD, SECOND_MA_PERIOD, ATR_PERIOD) + 30

    if df is None or df.empty:
        return None

    close_check = df["Close"].dropna()

    # 資料不夠算完整條件(SMA87/SMA284),但夠算新股簡化規則 → 走新股track
    if len(close_check) < min_len:
        if NEW_LISTING_ENABLE:
            return _evaluate_new_listing(df, ticker, name)
        return None

    close = close_check

    ma15 = close.rolling(BIAS_MA_PERIOD).mean()
    ma87 = close.rolling(LONG_MA_PERIOD).mean()
    bias = (close - ma15) / ma15 * 100.0

    # --- 條件1:近 N 日內乖離觸及門檻 ---
    recent_bias = bias.tail(LOOKBACK_DAYS)
    if recent_bias.isna().all():
        return None

    if BIAS_DIRECTION == "up":
        hit = recent_bias.max() >= BIAS_THRESHOLD
        trigger_val = recent_bias.max()
    elif BIAS_DIRECTION == "down":
        hit = recent_bias.min() <= -BIAS_THRESHOLD
        trigger_val = recent_bias.min()
    else:
        hit_up = recent_bias.max() >= BIAS_THRESHOLD
        hit_down = recent_bias.min() <= -BIAS_THRESHOLD
        hit = hit_up or hit_down
        trigger_val = recent_bias.max() if abs(recent_bias.max()) >= abs(recent_bias.min()) else recent_bias.min()

    if not hit:
        return None

    trigger_date = recent_bias.idxmax() if trigger_val > 0 else recent_bias.idxmin()

    # --- 條件2:最新收盤 > SMA87 ---
    latest_close = close.iloc[-1]
    latest_ma87 = ma87.iloc[-1]
    if pd.isna(latest_ma87) or latest_close <= latest_ma87:
        return None

    # --- 條件3:近 MA87_BREACH_LOOKBACK 日內不得跌破 SMA87 ---
    recent_close_87 = close.tail(MA87_BREACH_LOOKBACK)
    recent_ma87_87 = ma87.tail(MA87_BREACH_LOOKBACK)
    if (recent_close_87 < recent_ma87_87).any():
        return None

    # --- 條件4:SMA87 > SMA{SECOND_MA_PERIOD} ---
    latest_ma_second = None
    if REQUIRE_MA_ALIGNMENT:
        ma_second = close.rolling(SECOND_MA_PERIOD).mean()
        latest_ma_second = ma_second.iloc[-1]
        if pd.isna(latest_ma_second) or latest_ma87 <= latest_ma_second:
            return None

    # --- ATR14(同時算絕對值與佔股價百分比) ---
    atr = _calc_atr(df, ATR_PERIOD)
    latest_atr = atr.iloc[-1]
    latest_atr_pct = (latest_atr / latest_close * 100.0) if not pd.isna(latest_atr) and latest_close else None

    # --- 條件5:ATR14絕對值 與 佔股價百分比,兩個門檻都要通過才留下 ---
    if REQUIRE_ATR_MIN:
        if pd.isna(latest_atr) or latest_atr < ATR_MIN_THRESHOLD:
            return None
        if latest_atr_pct is None or latest_atr_pct < ATR_MIN_PCT_THRESHOLD:
            return None

    return {
        "ticker": ticker,
        "name": name,
        "close": round(float(latest_close), 2),
        "ma87": round(float(latest_ma87), 2),
        "ma_second": round(float(latest_ma_second), 2) if latest_ma_second is not None else None,
        "atr14": round(float(latest_atr), 2) if not pd.isna(latest_atr) else None,
        "atr14_pct": round(float(latest_atr_pct), 2) if latest_atr_pct is not None else None,
        "bias_pct": round(float(trigger_val), 2),
        "bias_date": trigger_date.strftime("%Y-%m-%d"),
        "as_of": close.index[-1].strftime("%Y-%m-%d"),
    }


def _fetch_batch(tickers: list[str]) -> dict[str, pd.DataFrame]:
    """一次網路請求下載一批股票的歷史資料,回傳 {ticker: DataFrame}"""
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

    results.sort(key=lambda r: abs(r["bias_pct"]), reverse=True)
    return results
