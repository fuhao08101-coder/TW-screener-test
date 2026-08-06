"""
核心篩選邏輯:
  條件1:最近 LOOKBACK_DAYS 個交易日內,曾出現「收盤價相對15MA乖離率 >= BIAS_THRESHOLD%」
  條件2:最新一根還原日K收盤價 > SMA87
  條件3:最近 MA87_BREACH_LOOKBACK 個交易日內,收盤價不得曾經跌破87MA(只看收盤價,不看盤中)
  條件4(新增):SMA87 > SMA287(中期均線站上長期均線,多頭排列結構)

還原日K:使用 yfinance auto_adjust=True,會依除權息回推調整 OHLC。
"""
from __future__ import annotations
import time
import pandas as pd
import yfinance as yf

# ------- 可調參數 -------
LOOKBACK_DAYS = 15          # 條件1:近幾個交易日內找乖離觸發
BIAS_MA_PERIOD = 15         # 15MA
BIAS_THRESHOLD = 20.0       # 乖離門檻(%)
LONG_MA_PERIOD = 87         # SMA87
BIAS_DIRECTION = "up"       # "up"=只抓正乖離(急漲) / "down"=只抓負乖離(急跌) / "both"=兩者都抓

MA87_BREACH_LOOKBACK = 20   # 條件3:近幾個交易日內不得跌破87MA(用收盤價判斷)

SECOND_MA_PERIOD = 287      # 條件4(新增):第二條均線天數
REQUIRE_MA_ALIGNMENT = True # 是否啟用「SMA87 > SMA{SECOND_MA_PERIOD}」濾網

HISTORY_PERIOD = "2y"       # 抓多久的歷史資料(算287MA需要更長歷史,含緩衝)
REQUEST_SLEEP = 0.3         # 每檔股票間的延遲,避免被限流
# ------------------------


def fetch_history(ticker: str) -> pd.DataFrame | None:
    try:
        df = yf.Ticker(ticker).history(period=HISTORY_PERIOD, auto_adjust=True)
        min_len = max(LONG_MA_PERIOD, SECOND_MA_PERIOD) + 30
        if df is None or df.empty or len(df) < min_len:
            return None
        return df
    except Exception:
        return None


def evaluate(ticker: str, name: str) -> dict | None:
    """回傳符合條件的股票資訊,不符合則回傳 None"""
    df = fetch_history(ticker)
    if df is None:
        return None

    close = df["Close"]
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

    # --- 條件3:近 MA87_BREACH_LOOKBACK 日內不得跌破 SMA87(收盤價判斷) ---
    recent_close_87 = close.tail(MA87_BREACH_LOOKBACK)
    recent_ma87_87 = ma87.tail(MA87_BREACH_LOOKBACK)
    if (recent_close_87 < recent_ma87_87).any():
        return None

    # --- 條件4(新增):SMA87 > SMA{SECOND_MA_PERIOD} ---
    latest_ma287 = None
    if REQUIRE_MA_ALIGNMENT:
        ma287 = close.rolling(SECOND_MA_PERIOD).mean()
        latest_ma287 = ma287.iloc[-1]
        if pd.isna(latest_ma287) or latest_ma87 <= latest_ma287:
            return None

    return {
        "ticker": ticker,
        "name": name,
        "close": round(float(latest_close), 2),
        "ma87": round(float(latest_ma87), 2),
        "ma287": round(float(latest_ma287), 2) if latest_ma287 is not None else None,
        "bias_pct": round(float(trigger_val), 2),
        "bias_date": trigger_date.strftime("%Y-%m-%d"),
        "as_of": close.index[-1].strftime("%Y-%m-%d"),
    }


def scan_universe(universe: list[dict], progress: bool = True) -> list[dict]:
    results = []
    total = len(universe)
    for i, row in enumerate(universe, 1):
        if progress and i % 50 == 0:
            print(f"進度 {i}/{total}")
        try:
            hit = evaluate(row["ticker"], row["name"])
            if hit:
                hit["market"] = row["market"]
                results.append(hit)
        except Exception as e:
            print(f"[warn] {row['ticker']} 失敗: {e}")
        time.sleep(REQUEST_SLEEP)
    results.sort(key=lambda r: abs(r["bias_pct"]), reverse=True)
    return results
