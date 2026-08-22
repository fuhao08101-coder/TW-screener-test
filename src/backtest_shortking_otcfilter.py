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


def _close_trade(trades, ticker, market, variant, entry_date, entry_price, exit_date, exit_price, reason):
    ret_pct = (exit_price - entry_price) / entry_price * 100.0
    if abs(ret_pct) > SANITY_MAX_RETURN_PCT:
        return
    holding_days = (exit_date - entry_date).days
    trades.append({
        "ticker": ticker, "market": market, "variant": variant,
        "entry_date": entry_date.strftime("%Y-%m-%d"),
        "exit_date": exit_date.strftime("%Y-%m-%d"),
        "return_pct": round(float(ret_pct), 2),
        "holding_days": holding_days,
        "exit_reason": reason,
    })


def simulate_variant(close, high, low, ma15, entry_signal, ticker, market, variant, trades):
    dates = close.index
    min_len = 60

    in_position = False
    entry_price = None
    entry_date = None
    entry_idx = None
    signal_low = None
    highest_close = None
    days_since_new_high = 0
    trailing_low_level = None

    i = min_len
    while i < len(dates):
        d = dates[i]
        c = close.iloc[i]
        l = low.iloc[i]
        m15 = ma15.iloc[i]

        if in_position:
            if signal_low is not None and l < signal_low:
                _close_trade(trades, ticker, market, variant, entry_date, entry_price, d, signal_low, "停損(訊號日低點)")
                in_position = False
                i += 1
                continue
            if not pd.isna(m15) and c < m15:
                _close_trade(trades, ticker, market, variant, entry_date, entry_price, d, c, "停損(跌破15MA)")
                in_position = False
                i += 1
                continue

            holding_days = i - entry_idx

            if trailing_low_level is None:
                if c > highest_close:
                    highest_close = c
                if holding_days >= HOLD_DAYS_CHECKPOINT:
                    ret_pct_now = (c - entry_price) / entry_price * 100.0
                    if ret_pct_now < PROFIT_THRESHOLD_PCT:
                        _close_trade(trades, ticker, market, variant, entry_date, entry_price, d, c, "時間停損(未達3%)")
                        in_position = False
                        i += 1
                        continue
                    else:
                        highest_close = c
                        days_since_new_high = 0
                        if i >= 1:
                            trailing_low_level = low.iloc[i - 1]
            else:
                if c > highest_close:
                    highest_close = c
                    days_since_new_high = 0
                    if i >= 1:
                        trailing_low_level = low.iloc[i - 1]
                else:
                    days_since_new_high += 1

                if days_since_new_high >= STALL_DAYS_LIMIT:
                    _close_trade(trades, ticker, market, variant, entry_date, entry_price, d, c, "停利(連續2天未創高)")
                    in_position = False
                    i += 1
                    continue
                if l < trailing_low_level:
                    _close_trade(trades, ticker, market, variant, entry_date, entry_price, d, c, "停利(跌破前K棒)")
                    in_position = False
                    i += 1
                    continue
        else:
            if bool(entry_signal.iloc[i]):
                in_position = True
                entry_price = c
                entry_date = d
                entry_idx = i
                signal_low = l
                highest_close = c
                days_since_new_high = 0
                trailing_low_level = None

        i += 1


def simulate_stock(df: pd.DataFrame, ticker: str, market: str, twse_regime: dict, otc_regime: dict) -> list[dict]:
    close = df["Close"].dropna()
    high = df["High"]
    low = df["Low"]

    min_len = max(SHORT_MA_PERIOD, ATR_PERIOD, BREAKOUT_LOOKBACK_DAYS) + 60
    if len(close) < min_len:
        return []

    daily_ratio = close / close.shift(1)
    if ((daily_ratio > SANITY_MAX_DAILY_JUMP) | (daily_ratio < 1 / SANITY_MAX_DAILY_JUMP)).any():
        return []

    ma15 = close.rolling(SHORT_MA_PERIOD).mean()
    bias = (close - ma15) / ma15 * 100.0
    atr = _calc_atr(df, ATR_PERIOD)
    recent_high = high.rolling(BREAKOUT_LOOKBACK_DAYS).max().shift(1)

    base_signal = (
        (close > ma15) & (atr >= ATR_MIN_THRESHOLD) &
        (bias >= BIAS_MIN_THRESHOLD) & (close > recent_high)
    ).fillna(False)

    # 現行版(無大盤濾網)
    signal_no_filter = base_signal

    # 加大盤濾網版:上市看加權指數、上櫃看櫃買指數,各自把關(對稱設計)
    regime_map = twse_regime if market == "TWSE" else otc_regime
    index_ok = pd.Series(
        [regime_map.get(dt.strftime("%Y-%m-%d"), False) for dt in close.index],
        index=close.index
    )
    signal_with_filter = base_signal & index_ok

    trades = []
    simulate_variant(close, high, low, ma15, signal_no_filter, ticker, market, "無大盤濾網", trades)
    simulate_variant(close, high, low, ma15, signal_with_filter, ticker, market, "加大盤濾網", trades)
    return trades


def run_backtest(max_stocks: int | None = None):
    twse_regime = fetch_index_regime(TWSE_INDEX_TICKER)
    otc_regime = fetch_index_regime(OTC_INDEX_TICKER)

    if len(twse_regime) < 200 or len(otc_regime) < 200:
        print("\n❌ 指數資料抓取不完整(重試後仍失敗),為避免浪費時間跑出無意義的結果,提早中止。")
        print(f"   加權指數資料筆數: {len(twse_regime)}, 櫃買指數資料筆數: {len(otc_regime)}")
        print("   請稍後重新執行一次(通常是暫時性的連線問題)。")
        return []

    # 順便印出兩指數的同步程度,直接回答「加權跟櫃買同不同步」這個問題
    common_dates = set(twse_regime.keys()) & set(otc_regime.keys())
    if common_dates:
        agree = sum(1 for d in common_dates if twse_regime[d] == otc_regime[d])
        print(f"\n【指數同步程度】共同交易日 {len(common_dates)} 天,"
              f"兩指數「是否站上15MA」判斷一致的天數: {agree} 天({agree/len(common_dates)*100:.1f}%)")

    print("抓取股票清單...")
    universe = get_universe(include_otc=True)
    if max_stocks:
        universe = universe[:max_stocks]
    print(f"共 {len(universe)} 檔,開始回測(大盤濾網 vs 無大盤濾網)...")

    all_tickers = [row["ticker"] for row in universe]
    ticker_market = {row["ticker"]: row["market"] for row in universe}
    batches = [all_tickers[i:i + BATCH_SIZE] for i in range(0, len(all_tickers), BATCH_SIZE)]

    all_trades = []
    done = 0
    for batch_idx, batch in enumerate(batches, 1):
        print(f"批次 {batch_idx}/{len(batches)}(已處理 {done}/{len(all_tickers)} 檔)")
        batch_data = _fetch_batch(batch)
        for t in batch:
            done += 1
            df = batch_data.get(t)
            if df is None:
                continue
            try:
                trades = simulate_stock(df, t, ticker_market.get(t, "?"), twse_regime, otc_regime)
                all_trades.extend(trades)
            except Exception as e:
                print(f"[warn] {t} 回測失敗: {e}")
        time.sleep(BATCH_SLEEP)

    print(f"\n產生 {len(all_trades)} 筆交易(2種組合合計)。")
    return all_trades


def _stats_for(trades: list[dict], label: str):
    if not trades:
        print(f"  【{label}】沒有交易紀錄。")
        return
    total = len(trades)
    wins = [t for t in trades if t["return_pct"] > 0]
    losses = [t for t in trades if t["return_pct"] <= 0]
    win_rate = len(wins) / total * 100
    avg_win = sum(t["return_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["return_pct"] for t in losses) / len(losses) if losses else 0
    avg_holding = sum(t["holding_days"] for t in trades) / total
    expectancy = (len(wins) / total) * avg_win + (len(losses) / total) * avg_loss
    print(f"  【{label}】{total}筆, 勝率{win_rate:.1f}%, 平均獲利+{avg_win:.2f}%, "
          f"平均虧損{avg_loss:.2f}%, 期望值{expectancy:+.2f}%, 平均持有{avg_holding:.1f}天")


def print_report(trades: list[dict]):
    print("\n" + "=" * 70)
    print("上市/上櫃各自疊加自己的大盤指數15MA環境濾網(對稱設計)")
    print("=" * 70)

    for v in ["無大盤濾網", "加大盤濾網"]:
        v_trades = [t for t in trades if t["variant"] == v]
        print(f"\n--- {v} ---")
        _stats_for(v_trades, f"{v} / 全部")
        twse = [t for t in v_trades if t["market"] == "TWSE"]
        tpex = [t for t in v_trades if t["market"] == "TPEX"]
        _stats_for(twse, f"{v} / 上市(理論上兩版應該一樣,驗證用)")
        _stats_for(tpex, f"{v} / 上櫃(這是真正的比較重點)")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-stocks", type=int, default=None)
    args = parser.parse_args()

    trades = run_backtest(max_stocks=args.max_stocks)
    print_report(trades)
