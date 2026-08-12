"""
回測工具 v8:進出場邏輯完全比照V2表現最好的版本(6天時間停損檢查、+3%門檻、
兩階段停利:跌破前一根K棒低點 或 連續3天未創新高)。

【唯一改動:篩選條件拿掉「近15日未破87MA」這一條】
原本 backtest.py 的 compute_eligible_mask 有5個條件:
  cond1: 近30日內15MA乖離 >= 20%
  cond2: 收盤 > SMA87
  cond3: 近15日未破87MA          ← v8拿掉這條
  cond4: SMA87 > SMA284
  cond5: ATR14絕對值>=9 且 佔股價>=1.5%
v8只用 cond1 + cond2 + cond4 + cond5,只要乖離夠大、ATR夠、整體結構是多頭
(收盤>87MA、87MA>284MA),就算最近15天內曾經跌破過87MA也算數,不排除。

進場、停損、出場邏輯完全跟V2一樣,沒有改動。

還原日K:使用 yfinance auto_adjust=True。
"""
from __future__ import annotations
import sys
import os
import time
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from universe import get_universe
from backtest import (
    _fetch_batch, _calc_atr,
    BATCH_SIZE, BATCH_SLEEP, HISTORY_PERIOD, MA_SHORT, MA_LONG,
    BIAS_LOOKBACK_DAYS, BIAS_MA_PERIOD, BIAS_THRESHOLD,
    LONG_MA_PERIOD, SECOND_MA_PERIOD, ATR_PERIOD,
    ATR_MIN_THRESHOLD, ATR_MIN_PCT_THRESHOLD,
)

HOLD_DAYS_CHECKPOINT = 6   # 跟V2最佳版本一致
PROFIT_THRESHOLD_PCT = 3.0
STALL_DAYS_LIMIT = 3


def compute_eligible_mask_v8(df: pd.DataFrame) -> pd.Series:
    """跟 backtest.py 的 compute_eligible_mask 一樣,但拿掉「近15日未破87MA」這條"""
    close = df["Close"].dropna()
    ma15 = close.rolling(BIAS_MA_PERIOD).mean()
    ma87 = close.rolling(LONG_MA_PERIOD).mean()
    ma284 = close.rolling(SECOND_MA_PERIOD).mean()
    bias = (close - ma15) / ma15 * 100.0
    atr = _calc_atr(df, ATR_PERIOD)
    atr_pct = atr / close * 100.0

    bias_window_max = bias.rolling(BIAS_LOOKBACK_DAYS).max()
    cond1 = bias_window_max >= BIAS_THRESHOLD
    cond2 = close > ma87
    cond4 = ma87 > ma284
    cond5 = (atr >= ATR_MIN_THRESHOLD) & (atr_pct >= ATR_MIN_PCT_THRESHOLD)

    eligible = cond1 & cond2 & cond4 & cond5
    return eligible.fillna(False)


def simulate_trades_v8(df: pd.DataFrame, ticker: str) -> list[dict]:
    close = df["Close"].dropna()
    high = df["High"]
    low = df["Low"]

    min_len = max(LONG_MA_PERIOD, SECOND_MA_PERIOD, ATR_PERIOD) + 30
    if len(close) < min_len:
        return []

    ma15 = close.rolling(MA_SHORT).mean()
    ma43 = close.rolling(MA_LONG).mean()
    eligible = compute_eligible_mask_v8(df)

    trades = []
    dates = close.index

    in_position = False
    entry_price = None
    entry_date = None
    entry_idx = None
    stop_loss_level = None
    activated = False
    highest_close = None
    days_since_new_high = 0
    trailing_stop_level = None

    pending_setup = False
    setup_window_low = None
    setup_days_count = 0
    MAX_SETUP_DAYS = 20

    i = min_len
    while i < len(dates):
        d = dates[i]
        c = close.iloc[i]
        h = high.iloc[i]
        l = low.iloc[i]
        m15 = ma15.iloc[i]
        m43 = ma43.iloc[i]
        elig = bool(eligible.iloc[i]) if i < len(eligible) else False

        if in_position:
            if stop_loss_level is not None and c < stop_loss_level:
                _close_trade(trades, ticker, entry_date, entry_price, d, c, "停損")
                in_position = False
                activated = False
                i += 1
                continue

            holding_days = i - entry_idx

            if not activated:
                if holding_days >= HOLD_DAYS_CHECKPOINT:
                    ret_pct = (c - entry_price) / entry_price * 100.0
                    if ret_pct < PROFIT_THRESHOLD_PCT:
                        _close_trade(trades, ticker, entry_date, entry_price, d, c, "時間停損(未達3%)")
                        in_position = False
                        i += 1
                        continue
                    else:
                        activated = True
                        highest_close = c
                        days_since_new_high = 0
                        if i >= 1:
                            trailing_stop_level = low.iloc[i - 1]
            else:
                if c > highest_close:
                    highest_close = c
                    days_since_new_high = 0
                    if i >= 1:
                        trailing_stop_level = low.iloc[i - 1]
                else:
                    days_since_new_high += 1

                if trailing_stop_level is not None and c < trailing_stop_level:
                    _close_trade(trades, ticker, entry_date, entry_price, d, c,
                                 "停利(跌破創高前一根K棒低點)")
                    in_position = False
                    activated = False
                    i += 1
                    continue

                if days_since_new_high >= STALL_DAYS_LIMIT:
                    _close_trade(trades, ticker, entry_date, entry_price, d, c,
                                 "停利(連續3天未創新高)")
                    in_position = False
                    activated = False
                    i += 1
                    continue

        else:
            if pending_setup:
                if i >= 1 and h > high.iloc[i - 1]:
                    in_position = True
                    entry_price = high.iloc[i - 1]
                    entry_date = d
                    entry_idx = i
                    stop_loss_level = setup_window_low
                    pending_setup = False
                    setup_window_low = None
                    setup_days_count = 0
                    i += 1
                    continue

                setup_window_low = min(setup_window_low, l)
                setup_days_count += 1
                if setup_days_count >= MAX_SETUP_DAYS:
                    pending_setup = False
                    setup_window_low = None
                    setup_days_count = 0

            if not pending_setup and elig:
                touched_or_broke = (not pd.isna(m15) and l <= m15) or (not pd.isna(m43) and l <= m43)
                if touched_or_broke:
                    pending_setup = True
                    setup_window_low = l
                    setup_days_count = 1

        i += 1

    return trades


def _close_trade(trades, ticker, entry_date, entry_price, exit_date, exit_price, reason):
    ret_pct = (exit_price - entry_price) / entry_price * 100.0
    holding_days = (exit_date - entry_date).days
    trades.append({
        "ticker": ticker,
        "entry_date": entry_date.strftime("%Y-%m-%d"),
        "exit_date": exit_date.strftime("%Y-%m-%d"),
        "entry_price": round(float(entry_price), 2),
        "exit_price": round(float(exit_price), 2),
        "return_pct": round(float(ret_pct), 2),
        "holding_days": holding_days,
        "exit_reason": reason,
    })


def run_backtest_v8(max_stocks: int | None = None):
    print("抓取股票清單...")
    universe = get_universe(include_otc=True)
    if max_stocks:
        universe = universe[:max_stocks]
    print(f"共 {len(universe)} 檔,開始回測(v8:V2最佳版本,拿掉近15日未破87MA條件)...")

    all_tickers = [row["ticker"] for row in universe]
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
                trades = simulate_trades_v8(df, t)
                all_trades.extend(trades)
            except Exception as e:
                print(f"[warn] {t} 回測失敗: {e}")
        time.sleep(BATCH_SLEEP)

    print(f"\n產生 {len(all_trades)} 筆交易。")
    return all_trades


def _stats_for(trades: list[dict], label: str):
    if not trades:
        print(f"\n【{label}】沒有交易紀錄。")
        return

    total = len(trades)
    wins = [t for t in trades if t["return_pct"] > 0]
    losses = [t for t in trades if t["return_pct"] <= 0]
    win_rate = len(wins) / total * 100
    avg_return = sum(t["return_pct"] for t in trades) / total
    avg_win = sum(t["return_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["return_pct"] for t in losses) / len(losses) if losses else 0
    avg_holding = sum(t["holding_days"] for t in trades) / total
    expectancy = (len(wins) / total) * avg_win + (len(losses) / total) * avg_loss

    print(f"\n【{label}】")
    print(f"  總交易筆數: {total}")
    print(f"  勝率: {win_rate:.1f}%({len(wins)}勝 / {len(losses)}敗)")
    print(f"  平均報酬率: {avg_return:.2f}%")
    print(f"  平均獲利: +{avg_win:.2f}%  平均虧損: {avg_loss:.2f}%")
    print(f"  期望值: {expectancy:.2f}%")
    print(f"  平均持有天數: {avg_holding:.1f} 天")


def print_stats_v8(trades: list[dict]):
    print("\n" + "=" * 60)
    print("回測結果統計 v8(V2最佳版本,篩選條件拿掉「近15日未破87MA」)")
    print("=" * 60)

    print("\n【與V2原版對照】")
    print("  V2原版(含近15日未破87MA條件): 1974筆, 勝率41.7%, 期望值+2.23%")

    _stats_for(trades, "v8 全部交易")

    print(f"\n--- 出場原因分布 ---")
    reason_counts = {}
    for t in trades:
        r = t.get("exit_reason", "未知")
        reason_counts[r] = reason_counts.get(r, 0) + 1
    for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
        pct = count / len(trades) * 100 if trades else 0
        avg_ret = sum(t["return_pct"] for t in trades if t.get("exit_reason") == reason) / count
        print(f"  {reason}: {count}筆({pct:.1f}%), 平均報酬 {avg_ret:+.2f}%")

    print("\n" + "=" * 60)
    print(f"\n報酬率最好的10筆交易:")
    for t in sorted(trades, key=lambda x: x["return_pct"], reverse=True)[:10]:
        print(f"  {t['ticker']}: {t['entry_date']}進場 → {t['exit_date']}出場, "
              f"報酬 {t['return_pct']:+.2f}%, 持有{t['holding_days']}天, "
              f"出場原因={t.get('exit_reason')}")

    print(f"\n報酬率最差的10筆交易:")
    for t in sorted(trades, key=lambda x: x["return_pct"])[:10]:
        print(f"  {t['ticker']}: {t['entry_date']}進場 → {t['exit_date']}出場, "
              f"報酬 {t['return_pct']:+.2f}%, 持有{t['holding_days']}天, "
              f"出場原因={t.get('exit_reason')}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-stocks", type=int, default=None)
    args = parser.parse_args()

    trades = run_backtest_v8(max_stocks=args.max_stocks)
    print_stats_v8(trades)
