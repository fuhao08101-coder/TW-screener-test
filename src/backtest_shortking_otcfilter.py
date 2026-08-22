"""
短線王(組合C)疊加「各自市場大盤指數15MA環境濾網」的對照回測。

新增規則(這次改成對稱,上市櫃都各自檢查自己的大盤指數):
  上市(TWSE)股票要進場,當天加權指數(^TWII)收盤價必須站上指數自己的15MA
  上櫃(TPEX)股票要進場,當天櫃買指數(^TWOII)收盤價必須站上指數自己的15MA
兩個指數各自獨立判斷,不是用同一個指數套用在全部股票上——這樣才能檢驗
「加權跟櫃買到底同不同步」以及「大盤結構性分歧時,哪個市場該做」這兩個問題。

比較兩組:
  現行版(無大盤濾網):維持原本組合C邏輯,上市櫃都不看指數
  加大盤濾網版:上市看加權指數15MA、上櫃看櫃買指數15MA,各自把關

資料範圍:近5年,跟之前的V2/短線王對照用同樣期間。
"""
from __future__ import annotations
import sys
import os
import time
import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(__file__))
from universe import get_universe

HISTORY_PERIOD = "5y"
BATCH_SIZE = 150
BATCH_SLEEP = 1.0

ATR_PERIOD = 14
ATR_MIN_THRESHOLD = 8.0
SHORT_MA_PERIOD = 15
BIAS_MIN_THRESHOLD = 8.0
BREAKOUT_LOOKBACK_DAYS = 5

HOLD_DAYS_CHECKPOINT = 6
PROFIT_THRESHOLD_PCT = 3.0
STALL_DAYS_LIMIT = 2

SANITY_MAX_RETURN_PCT = 80.0
SANITY_MAX_DAILY_JUMP = 3.0

OTC_INDEX_TICKER = "^TWOII"   # 櫃買指數
TWSE_INDEX_TICKER = "^TWII"   # 加權指數


def _calc_atr(df: pd.DataFrame, period: int) -> pd.Series:
    high = df["High"]
    low = df["Low"]
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        (high - low), (high - prev_close).abs(), (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def fetch_index_regime(ticker: str, max_retries: int = 3) -> dict:
    """回傳 {日期字串: True/False},True代表當天該指數收盤 > 指數自己的15MA。
    ^TWOII這個代號用 period="5y" 這種相對期間查詢,實測發現系統性地只回傳1天資料
    (重試3次結果一致,不是偶發性連線問題)。改用明確的開始/結束日期查詢,
    這種寫法有時候在yfinance後端走的是不同的資料路徑,值得一試。
    """
    import time as _time
    from datetime import date, timedelta

    end_date = date.today()
    start_date = end_date - timedelta(days=365 * 5 + 30)  # 抓5年多一點,確保足夠

    df = None
    for attempt in range(1, max_retries + 1):
        print(f"抓取指數({ticker})歷史資料...(第{attempt}次嘗試,明確日期區間查詢)")
        try:
            df = yf.Ticker(ticker).history(start=start_date, end=end_date, auto_adjust=True)
        except Exception as e:
            print(f"[warn] 指數{ticker}第{attempt}次抓取失敗: {e}")
            df = None

        if df is not None and not df.empty and len(df) >= 200:
            break

        if df is not None and not df.empty:
            print(f"[warn] 指數{ticker}第{attempt}次只抓到 {len(df)} 天,判斷資料不完整,準備重試")
        if attempt < max_retries:
            _time.sleep(5)

    # 如果日期區間查詢還是失敗,最後試一次 yf.download(對某些代號走的路徑不同)
    if df is None or df.empty or len(df) < 200:
        print(f"[warn] 指數{ticker}日期區間查詢仍不足,改試 yf.download() 方式...")
        try:
            df = yf.download(ticker, start=start_date, end=end_date, auto_adjust=True, progress=False)
        except Exception as e:
            print(f"[warn] 指數{ticker} yf.download() 也失敗: {e}")
            df = None

    if df is None or df.empty or len(df) < 200:
        print(f"[warn] 指數{ticker}所有方式都嘗試過,資料仍不足,回傳空結果")
        return {}

    close = df["Close"].dropna()
    if hasattr(close, "columns"):  # yf.download有時候回傳多層欄位,取第一欄保險
        close = close.iloc[:, 0]
    ma15 = close.rolling(SHORT_MA_PERIOD).mean()
    regime = (close > ma15).fillna(False)

    out = {}
    for dt, val in regime.items():
        out[dt.strftime("%Y-%m-%d")] = bool(val)
    print(f"指數{ticker}環境資料:共 {len(out)} 個交易日,其中站上15MA的天數: {sum(out.values())} 天")
    return out


def _fetch_batch(tickers: list[str]) -> dict[str, pd.DataFrame]:
    out = {}
    try:
        data = yf.download(
            tickers=tick
