"""
短線王(組合C:收盤站上5日高+乖離8%)疊加外資融資濾網的三方比較回測。

比較三種版本:
  無濾網:只看價格條件(組合C),不管外資融資
  雙增(嚴格):只看「進場當天」外資買超+融資增加,不用9天回顧、沒有黏性邏輯
  雙買(寬鬆,現行正式版邏輯):近9個交易日內任一天觸發過雙買、且之後沒出現雙減就合格

資料範圍:近1年(使用者做短線,近1年波動已足夠,不需要拉到5年)

【效能設計】外資融資資料是「一天抓一次全市場」,不是「一檔股票抓一次」,
所以抓一年份(~250個交易日)的歷史資料,只需要約250x4次請求,比抓股價快很多。
"""
from __future__ import annotations
import sys
import os
import time
from datetime import date, timedelta
import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(__file__))
from universe import get_universe
from institutional_flow import fetch_day_flow

HISTORY_PERIOD = "1y"
BATCH_SIZE = 150
BATCH_SLEEP = 1.0

ATR_PERIOD = 14
ATR_MIN_THRESHOLD = 8.0
SHORT_MA_PERIOD = 15
BIAS_MIN_THRESHOLD = 8.0
BREAKOUT_LOOKBACK_DAYS = 5   # 組合C:收盤站上5日高

DUAL_BUY_LOOKBACK_DAYS = 9   # 雙買(寬鬆版)的回顧天數

HOLD_DAYS_CHECKPOINT = 6
PROFIT_THRESHOLD_PCT = 3.0
STALL_DAYS_LIMIT = 2

SANITY_MAX_RETURN_PCT = 80.0
SANITY_MAX_DAILY_JUMP = 3.0

BACKTEST_DAYS = 365  # 抓多久的外資融資歷史(日曆天,近1年)


def _calc_atr(df: pd.DataFrame, period: int) -> pd.Series:
    high = df["High"]
    low = df["Low"]
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        (high - low), (high - prev_close).abs(), (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


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


def collect_flow_history(days: int = BACKTEST_DAYS):
    """抓近days個日曆天內,每個交易日的外資+融資資料,回傳依日期由舊到新排列的list"""
    print(f"開始抓取近{days}天的外資融資歷史資料(每天全市場一次,不是每檔股票一次)...")
    collected = []
    d = date.today()
    cutoff = d - timedelta(days=days)
    total_tried = 0
    while d >= cutoff:
        foreign_map, margin_map = fetch_day_flow(d)
        if foreign_map and margin_map:
            collected.append((d, foreign_map, margin_map))
        total_tried += 1
        if total_tried % 30 == 0:
            print(f"  已嘗試 {total_tried} 天,成功取得 {len(collected)} 個交易日的資料...")
        d -= timedelta(days=1)
    collected.reverse()
    print(f"外資融資歷史資料抓取完成,共 {len(collected)} 個有效交易日")
    return collected


def build_qualification_by_day(flow_history):
    """
    回傳兩個dict,key是日期字串(YYYY-MM-DD),value是當天合格的股票代號集合:
      loose_by_day: 雙買(寬鬆,9天回顧+黏性邏輯)
      strict_by_day: 雙增(嚴格,只看當天)
    """
    loose_by_day = {}
    strict_by_day = {}

    for i, (d, foreign_map, margin_map) in enumerate(flow_history):
        date_key = d.strftime("%Y-%m-%d")

        # 雙增(嚴格):只看當天
        strict_set = {
            code for code in foreign_map
            if foreign_map.get(code, 0) > 0 and margin_map.get(code, 0) > 0
        }
        strict_by_day[date_key] = strict_set

        # 雙買(寬鬆):近9個交易日回顧
        window = flow_history[max(0, i - DUAL_BUY_LOOKBACK_DAYS + 1):i + 1]
        window_codes = set()
        for _, fm, mm in window:
            window_codes.update(fm.keys())
            window_codes.update(mm.keys())

        loose_set = set()
        for code in window_codes:
            trigger_idx = None
            for idx, (_, fm, mm) in enumerate(window):
                if fm.get(code, 0) > 0 and mm.get(code, 0) > 0:
                    trigger_idx = idx
                    break
            if trigger_idx is None:
                continue
            dual_sell_after = False
            for idx in range(trigger_idx, len(window)):
                _, fm, mm = window[idx]
                if fm.get(code, 0) < 0 and mm.get(code, 0) < 0:
                    dual_sell_after = True
                    break
            if not dual_sell_after:
                loose_set.add(code)
        loose_by_day[date_key] = loose_set

    return loose_by_day, strict_by_day


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
                _close_trade(trades, ticker, market, variant, entry_date, entry_price, d, signal_low, "停損")
                in_position = False
                i += 1
                continue
            if not pd.isna(m15) and c < m15:
                _close_trade(trades, ticker, market, variant, entry_date, entry_price, d, c, "停損")
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
                        _close_trade(trades, ticker, market, variant, entry_date, entry_price, d, c, "時間停損")
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
                    _close_trade(trades, ticker, market, variant, entry_date, entry_price, d, c, "停利(未創高)")
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


def simulate_stock(df: pd.DataFrame, ticker: str, market: str, loose_by_day: dict, strict_by_day: dict) -> list[dict]:
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

    base_signal = (close > ma15) & (atr >= ATR_MIN_THRESHOLD) & \
                  (bias >= BIAS_MIN_THRESHOLD) & (close > recent_high)
    base_signal = base_signal.fillna(False)

    code = ticker.replace(".TWO", "").replace(".TW", "")

    # 無濾網
    signal_none = base_signal

    # 雙增(嚴格)、雙買(寬鬆):逐日查表
    signal_strict = pd.Series(False, index=close.index)
    signal_loose = pd.Series(False, index=close.index)
    for dt in close.index:
        date_key = dt.strftime("%Y-%m-%d")
        if code in strict_by_day.get(date_key, set()):
            signal_strict.loc[dt] = True
        if code in loose_by_day.get(date_key, set()):
            signal_loose.loc[dt] = True

    signal_strict = base_signal & signal_strict
    signal_loose = base_signal & signal_loose

    trades = []
    simulate_variant(close, high, low, ma15, signal_none, ticker, market, "無濾網", trades)
    simulate_variant(close, high, low, ma15, signal_strict, ticker, market, "雙增(嚴格)", trades)
    simulate_variant(close, high, low, ma15, signal_loose, ticker, market, "雙買(寬鬆)", trades)
    return trades


def run_backtest(max_stocks: int | None = None):
    flow_history = collect_flow_history(BACKTEST_DAYS)
    if len(flow_history) < 10:
        print("❌ 外資融資歷史資料太少,無法進行有意義的回測,提早結束")
        return []

    loose_by_day, strict_by_day = build_qualification_by_day(flow_history)

    print("抓取股票清單...")
    universe = get_universe(include_otc=True)
    if max_stocks:
        universe = universe[:max_stocks]
    print(f"共 {len(universe)} 檔,開始回測(3組合比較)...")

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
                trades = simulate_stock(df, t, ticker_market.get(t, "?"), loose_by_day, strict_by_day)
                all_trades.extend(trades)
            except Exception as e:
                print(f"[warn] {t} 回測失敗: {e}")
        time.sleep(BATCH_SLEEP)

    print(f"\n產生 {len(all_trades)} 筆交易(3種組合合計)。")
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
    print("組合C(收盤站上5日高+乖離8%)疊加外資融資濾網:三方比較(近1年)")
    print("=" * 70)
    for v in ["無濾網", "雙增(嚴格)", "雙買(寬鬆)"]:
        v_trades = [t for t in trades if t["variant"] == v]
        _stats_for(v_trades, v)

    print("\n" + "=" * 70)
    print("拆上市/上櫃")
    print("=" * 70)
    for v in ["無濾網", "雙增(嚴格)", "雙買(寬鬆)"]:
        v_trades = [t for t in trades if t["variant"] == v]
        twse = [t for t in v_trades if t["market"] == "TWSE"]
        tpex = [t for t in v_trades if t["market"] == "TPEX"]
        print(f"\n--- {v} ---")
        _stats_for(twse, f"{v} / 上市")
        _stats_for(tpex, f"{v} / 上櫃")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-stocks", type=int, default=None)
    args = parser.parse_args()

    trades = run_backtest(max_stocks=args.max_stocks)
    print_report(trades)
