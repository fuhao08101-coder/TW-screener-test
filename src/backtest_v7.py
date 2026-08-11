"""
回測工具 v7:用「乖離背離放空訊號」當進場條件,出場套用跟v6一樣的兩階段框架。

【進場訊號:乖離背離放空】(驗證邏輯見 screener_short_divergence.py)
1. 往前找近25個交易日內,乖離率(收盤價相對15MA)曾經 >10% 的那個「最高乖離參考日」
2. 當天「盤中最高價」超過「參考日的收盤價」(創新高,用盤中價比對,不是收盤價),
   且「當天的乖離率」比參考日小(背離、動能減弱)
3. 當天 ATR14 > 10
三個條件同時成立,當天收盤價直接進場放空(訊號本身就是確認事件,不用像V2/V6
那樣還要再等隔天突破確認)。

【出場:跟v6一樣的兩階段框架,停損防守線改為近3日高點+3%緩衝】
停損:用「進場前近3個交易日(含訊號日)的最高點,再加3%緩衝」當防守線,之後只要
盤中最高價突破這條線,當下停損回補。(v7原本用訊號日自己的高點當防守線太緊,
71.7%的交易都在這裡被洗出場,所以放寬成近3天高點+3%緩衝,減少雜訊洗盤)
階段1(時間停損):放空後持有滿6個交易日,未達+3%獲利就回補。
階段2(啟動後追蹤):追蹤最低收盤價,連續3天沒創新低就回補;或創新低後收盤漲破
前一根K棒高點就回補,兩者哪個先發生用哪個。

還原日K:使用 yfinance auto_adjust=True。
"""
from __future__ import annotations
import sys
import os
import time
from datetime import date
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from universe import get_universe
from backtest import (
    _fetch_batch, _calc_atr,
    BATCH_SIZE, BATCH_SLEEP, HISTORY_PERIOD,
)

# 進場訊號參數
BIAS_MA_PERIOD = 15
REF_LOOKBACK_DAYS = 25
REF_BIAS_THRESHOLD = 10.0
ATR_PERIOD = 14
ATR_THRESHOLD = 10.0

# 出場參數(跟v6一致)
HOLD_DAYS_CHECKPOINT = 6
PROFIT_THRESHOLD_PCT = 3.0
STALL_DAYS_LIMIT = 3
STOP_LOOKBACK_DAYS = 3    # 停損防守線:抓近幾天的最高點
STOP_BUFFER_PCT = 3.0     # 停損防守線:額外加多少%緩衝


def simulate_trades_v7(df: pd.DataFrame, ticker: str) -> list[dict]:
    close = df["Close"].dropna()
    high = df["High"]
    low = df["Low"]

    min_len = BIAS_MA_PERIOD + REF_LOOKBACK_DAYS + 5
    if len(close) < min_len:
        return []

    ma15 = close.rolling(BIAS_MA_PERIOD).mean()
    bias = (close - ma15) / ma15 * 100.0
    atr14 = _calc_atr(df, ATR_PERIOD)

    trades = []
    dates = close.index

    in_short = False
    entry_price = None
    entry_date = None
    entry_idx = None
    stop_loss_level = None  # 進場防守線(近3日高點+3%緩衝)

    activated = False
    lowest_close = None
    days_since_new_low = 0
    trailing_stop_level = None

    i = min_len
    while i < len(dates):
        d = dates[i]
        c = close.iloc[i]
        h = high.iloc[i]
        b = bias.iloc[i]
        a = atr14.iloc[i]

        if in_short:
            # 停損:盤中最高價突破防守線(近3日最高點+3%緩衝)
            if stop_loss_level is not None and h > stop_loss_level:
                _close_trade(trades, ticker, entry_date, entry_price, d, stop_loss_level,
                             "停損(盤中突破近3日高點+3%緩衝)", side="short")
                in_short = False
                activated = False
                i += 1
                continue

            holding_days = i - entry_idx

            if not activated:
                if holding_days >= HOLD_DAYS_CHECKPOINT:
                    ret_pct = (entry_price - c) / entry_price * 100.0
                    if ret_pct < PROFIT_THRESHOLD_PCT:
                        _close_trade(trades, ticker, entry_date, entry_price, d, c,
                                     "時間停損(未達3%)", side="short")
                        in_short = False
                        i += 1
                        continue
                    else:
                        activated = True
                        lowest_close = c
                        days_since_new_low = 0
                        if i >= 1:
                            trailing_stop_level = high.iloc[i - 1]
            else:
                if c < lowest_close:
                    lowest_close = c
                    days_since_new_low = 0
                    if i >= 1:
                        trailing_stop_level = high.iloc[i - 1]
                else:
                    days_since_new_low += 1

                if trailing_stop_level is not None and c > trailing_stop_level:
                    _close_trade(trades, ticker, entry_date, entry_price, d, c,
                                 "停利(突破創低前一根K棒高點)", side="short")
                    in_short = False
                    activated = False
                    i += 1
                    continue

                if days_since_new_low >= STALL_DAYS_LIMIT:
                    _close_trade(trades, ticker, entry_date, entry_price, d, c,
                                 "停利(連續3天未創新低)", side="short")
                    in_short = False
                    activated = False
                    i += 1
                    continue

        else:
            # 尋找乖離背離放空訊號
            window_start = max(0, i - REF_LOOKBACK_DAYS)
            window_bias = bias.iloc[window_start:i]
            if not window_bias.isna().all() and not pd.isna(b) and not pd.isna(a):
                ref_pos = window_bias.values.argmax()
                ref_idx = window_start + ref_pos
                ref_bias = bias.iloc[ref_idx]
                ref_close = close.iloc[ref_idx]

                if not pd.isna(ref_bias) and ref_bias > REF_BIAS_THRESHOLD:
                    made_new_high = h > ref_close
                    bias_shrunk = b < ref_bias
                    atr_ok = a > ATR_THRESHOLD

                    if made_new_high and bias_shrunk and atr_ok:
                        in_short = True
                        entry_price = c
                        entry_date = d
                        entry_idx = i
                        lookback_start = max(0, i - (STOP_LOOKBACK_DAYS - 1))
                        recent_high = high.iloc[lookback_start:i + 1].max()  # 近3天(含訊號日)最高點
                        stop_loss_level = recent_high * (1 + STOP_BUFFER_PCT / 100.0)

        i += 1

    return trades


def _close_trade(trades, ticker, entry_date, entry_price, exit_date, exit_price, reason, side="short"):
    if side == "short":
        ret_pct = (entry_price - exit_price) / entry_price * 100.0
    else:
        ret_pct = (exit_price - entry_price) / entry_price * 100.0
    holding_days = (exit_date - entry_date).days
    trades.append({
        "ticker": ticker,
        "side": side,
        "entry_date": entry_date.strftime("%Y-%m-%d"),
        "exit_date": exit_date.strftime("%Y-%m-%d"),
        "entry_price": round(float(entry_price), 2),
        "exit_price": round(float(exit_price), 2),
        "return_pct": round(float(ret_pct), 2),
        "holding_days": holding_days,
        "exit_reason": reason,
    })


def run_backtest_v7(max_stocks: int | None = None):
    print("抓取股票清單...")
    universe = get_universe(include_otc=True)
    if max_stocks:
        universe = universe[:max_stocks]
    print(f"共 {len(universe)} 檔,開始回測(v7:乖離背離放空訊號)...")

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
                trades = simulate_trades_v7(df, t)
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


def print_stats_v7(trades: list[dict]):
    print("\n" + "=" * 60)
    print("回測結果統計 v7(乖離背離放空訊號,出場沿用v6兩階段框架)")
    print("=" * 60)

    print("\n【與前幾次放空版本對照】")
    print("  v6(V2純放空鏡像,碰均線後跌破前低進場): 見前次結果")

    _stats_for(trades, "v7 全部交易")

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

    trades = run_backtest_v7(max_stocks=args.max_stocks)
    print_stats_v7(trades)
