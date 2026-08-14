"""
回測工具 v11:V10-C 多單邏輯 + 觸發「跌破創高前一根K棒低點」停利時反手放空(比照v4)。

【多單部分:完全比照 V10-C,沒有改動】
進場:ATR14≥8、收盤>15MA、乖離(收盤相對15MA)≥13%,當天收盤進場
停損:盤中跌破訊號日低點 / 收盤跌破15MA
停利:連續2天未創收盤新高 / 盤中跌破「創高那天前一根K棒的低點」(動態防守線)

【反手放空部分:比照v4的做法】
只有「跌破創高前一根K棒低點」這個停利出場觸發時,才反手放空(「連續2天未創新高」
出場、以及一開始的停損出場,都不觸發反手,直接出場)。
反手進場價:出場多單那天的收盤價。
放空停損:反手當下抓「最近3個交易日(含當天)的最高點」當防守線,之後只要盤中最高價
突破這條線,就停損回補。
放空回補(獲利了結):只要當天最低價碰到15MA,當天用收盤價回補。
(v10-C框架裡沒有43MA的概念,所以只用15MA,跟v4用15/43MA略有不同)

還原日K:使用 yfinance auto_adjust=True。
"""
from __future__ import annotations
import sys
import os
import time
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from universe import get_universe
from backtest import _fetch_batch, _calc_atr, BATCH_SIZE, BATCH_SLEEP, HISTORY_PERIOD

BIAS_MA_PERIOD = 15
BIAS_THRESHOLD_ENTRY = 13.0
ATR_PERIOD = 14
ATR_MIN_THRESHOLD = 8.0
STALL_DAYS_LIMIT = 2

SHORT_STOP_LOOKBACK_DAYS = 3  # 反手放空:防守停損線抓最近幾天的高點


def simulate_trades_v11(df: pd.DataFrame, ticker: str) -> list[dict]:
    close = df["Close"].dropna()
    high = df["High"]
    low = df["Low"]

    min_len = max(BIAS_MA_PERIOD, ATR_PERIOD) + 30
    if len(close) < min_len:
        return []

    ma15 = close.rolling(BIAS_MA_PERIOD).mean()
    bias = (close - ma15) / ma15 * 100.0
    atr14 = _calc_atr(df, ATR_PERIOD)

    trades = []
    dates = close.index

    # 多單狀態
    in_position = False
    entry_price = None
    entry_date = None
    signal_low = None
    highest_close = None
    days_since_new_high = 0
    trailing_low_level = None

    # 放空狀態
    in_short = False
    short_entry_price = None
    short_entry_date = None
    short_stop_level = None

    i = min_len
    while i < len(dates):
        d = dates[i]
        c = close.iloc[i]
        h = high.iloc[i]
        l = low.iloc[i]
        m15 = ma15.iloc[i]
        b = bias.iloc[i]
        a = atr14.iloc[i]

        if in_position:
            # 停損1:盤中最低價跌破訊號日低點
            if signal_low is not None and l < signal_low:
                _close_trade(trades, ticker, entry_date, entry_price, d, signal_low,
                             "停損(跌破訊號日低點)", side="long")
                in_position = False
                i += 1
                continue

            # 停損2:收盤跌破15MA
            if not pd.isna(m15) and c < m15:
                _close_trade(trades, ticker, entry_date, entry_price, d, c,
                             "停損(收盤跌破15MA)", side="long")
                in_position = False
                i += 1
                continue

            # 停利1:連續2天未創收盤新高,同時更新動態防守線
            if c > highest_close:
                highest_close = c
                days_since_new_high = 0
                if i >= 1:
                    trailing_low_level = low.iloc[i - 1]
            else:
                days_since_new_high += 1

            if days_since_new_high >= STALL_DAYS_LIMIT:
                _close_trade(trades, ticker, entry_date, entry_price, d, c,
                             "停利(連續2天未創新高)", side="long")
                in_position = False
                i += 1
                continue

            # 停利2:盤中跌破動態防守線 → 出場多單,反手放空
            if trailing_low_level is not None and l < trailing_low_level:
                _close_trade(trades, ticker, entry_date, entry_price, d, c,
                             "停利(跌破創高前一根K棒低點)", side="long")
                in_position = False

                in_short = True
                short_entry_price = c
                short_entry_date = d
                lookback_start = max(0, i - (SHORT_STOP_LOOKBACK_DAYS - 1))
                short_stop_level = high.iloc[lookback_start:i + 1].max()

                i += 1
                continue

        elif in_short:
            # 放空停損:盤中最高價突破防守線(最近3日高點)
            if short_stop_level is not None and h > short_stop_level:
                _close_trade(trades, ticker, short_entry_date, short_entry_price, d, short_stop_level,
                             "放空停損(盤中突破近期高點)", side="short")
                in_short = False
                i += 1
                continue

            # 放空回補:當天最低價碰到15MA
            if not pd.isna(m15) and l <= m15:
                _close_trade(trades, ticker, short_entry_date, short_entry_price, d, c,
                             "放空回補(觸及15MA)", side="short")
                in_short = False
                i += 1
                continue

        else:
            if not pd.isna(b) and not pd.isna(a) and not pd.isna(m15):
                cond_atr = a >= ATR_MIN_THRESHOLD
                cond_above_ma = c > m15
                cond_bias = b >= BIAS_THRESHOLD_ENTRY

                if cond_atr and cond_above_ma and cond_bias:
                    in_position = True
                    entry_price = c
                    entry_date = d
                    signal_low = l
                    highest_close = c
                    days_since_new_high = 0
                    trailing_low_level = None

        i += 1

    return trades


def _close_trade(trades, ticker, entry_date, entry_price, exit_date, exit_price, reason, side="long"):
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


def run_backtest_v11(max_stocks: int | None = None):
    print("抓取股票清單...")
    universe = get_universe(include_otc=True)
    if max_stocks:
        universe = universe[:max_stocks]
    print(f"共 {len(universe)} 檔,開始回測(v11:V10-C + 跌破前K棒低點反手放空)...")

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
                trades = simulate_trades_v11(df, t)
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


def print_stats_v11(trades: list[dict]):
    print("\n" + "=" * 60)
    print("回測結果統計 v11(V10-C + 跌破前K棒低點時反手放空)")
    print("=" * 60)

    print("\n【與V4對照(V2版本的反手放空)】")
    print("  v4放空(V2+反手,碰均線回補): 536筆, 勝率55.6%, 期望值+0.45%")

    long_trades = [t for t in trades if t.get("side", "long") == "long"]
    short_trades = [t for t in trades if t.get("side") == "short"]

    _stats_for(trades, "v11 全部交易(多單+空單)")
    _stats_for(long_trades, "v11 多單(應與v10-C一致)")
    _stats_for(short_trades, "v11 反手放空單")

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
        print(f"  [{t.get('side','long')}] {t['ticker']}: {t['entry_date']}進場 → {t['exit_date']}出場, "
              f"報酬 {t['return_pct']:+.2f}%, 持有{t['holding_days']}天, "
              f"出場原因={t.get('exit_reason')}")

    print(f"\n報酬率最差的10筆交易:")
    for t in sorted(trades, key=lambda x: x["return_pct"])[:10]:
        print(f"  [{t.get('side','long')}] {t['ticker']}: {t['entry_date']}進場 → {t['exit_date']}出場, "
              f"報酬 {t['return_pct']:+.2f}%, 持有{t['holding_days']}天, "
              f"出場原因={t.get('exit_reason')}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-stocks", type=int, default=None)
    args = parser.parse_args()

    trades = run_backtest_v11(max_stocks=args.max_stocks)
    print_stats_v11(trades)
