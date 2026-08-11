"""
回測工具 v5:基於 backtest_v4.py,唯一差異是「反手放空後的回補邏輯」。

【與 v4 完全相同的部分】:
- 多單進場、停損、第一階段時間停損(3天)、第二階段停利追蹤(含反手放空的觸發時機)
- 放空的防守停損線(最近3日高點,盤中突破就停損)
- 重複使用 backtest.py 的篩選條件判斷、鉅額交易分組、批次下載機制

【v5 唯一改動:放空的回補條件】
v4:只要當天最低價碰到15MA或43MA就回補(容易賺得太淺)
v5:改成跟多單停利邏輯對稱——
  - 持續追蹤「放空後的最低收盤價」,只要收盤價創新低,就把回補防守線更新成
    「創新低那天的前一根K棒高點」
  - 只要收盤價突破這條線,當天回補
  這樣可以讓有效下跌的空單繼續抱著,不會一碰到均線就提早獲利了結。

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
    _fetch_batch, compute_eligible_mask, fetch_block_trade_dates, tag_block_trade,
    BATCH_SIZE, BATCH_SLEEP, HISTORY_PERIOD, MA_SHORT, MA_LONG,
    LONG_MA_PERIOD, SECOND_MA_PERIOD, ATR_PERIOD,
)

HOLD_DAYS_CHECKPOINT = 3      # 第一階段:持有幾個交易日後檢查獲利門檻
PROFIT_THRESHOLD_PCT = 3.0    # 第一階段:獲利門檻(%)
STALL_DAYS_LIMIT = 3          # 第二階段:連續幾天沒創新高就出場(多單)
SHORT_STOP_LOOKBACK_DAYS = 3  # 反手放空:防守停損線抓最近幾天的高點


def simulate_trades_v5(df: pd.DataFrame, ticker: str) -> list[dict]:
    close = df["Close"].dropna()
    high = df["High"]
    low = df["Low"]

    min_len = max(LONG_MA_PERIOD, SECOND_MA_PERIOD, ATR_PERIOD) + 30
    if len(close) < min_len:
        return []

    ma15 = close.rolling(MA_SHORT).mean()
    ma43 = close.rolling(MA_LONG).mean()
    eligible = compute_eligible_mask(df)

    trades = []
    dates = close.index

    # 多單狀態
    in_position = False
    entry_price = None
    entry_date = None
    entry_idx = None
    stop_loss_level = None
    activated = False
    highest_close = None
    days_since_new_high = 0
    trailing_stop_level = None

    # 放空狀態
    in_short = False
    short_entry_price = None
    short_entry_date = None
    short_stop_level = None       # 防守停損線(近期3日高點,固定)
    short_lowest_close = None     # 放空後追蹤的最低收盤價(v5新增)
    short_trailing_level = None   # 回補防守線:創新低那天的前一根K棒高點(v5新增)

    # 候選訊號等待突破的狀態
    pending_setup = False
    setup_window_high = None
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
                _close_trade(trades, ticker, entry_date, entry_price, d, c, "停損", side="long")
                in_position = False
                activated = False
                i += 1
                continue

            holding_days = i - entry_idx

            if not activated:
                if holding_days >= HOLD_DAYS_CHECKPOINT:
                    ret_pct = (c - entry_price) / entry_price * 100.0
                    if ret_pct < PROFIT_THRESHOLD_PCT:
                        _close_trade(trades, ticker, entry_date, entry_price, d, c, "時間停損(未達3%)", side="long")
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
                    # 出場多單,反手放空
                    _close_trade(trades, ticker, entry_date, entry_price, d, c,
                                 "停利(跌破創高前一根K棒低點)", side="long")
                    in_position = False
                    activated = False

                    in_short = True
                    short_entry_price = c
                    short_entry_date = d
                    lookback_start = max(0, i - (SHORT_STOP_LOOKBACK_DAYS - 1))
                    short_stop_level = high.iloc[lookback_start:i + 1].max()
                    short_lowest_close = c
                    short_trailing_level = high.iloc[i - 1] if i >= 1 else None

                    i += 1
                    continue

                if days_since_new_high >= STALL_DAYS_LIMIT:
                    _close_trade(trades, ticker, entry_date, entry_price, d, c,
                                 "停利(連續3天未創新高)", side="long")
                    in_position = False
                    activated = False
                    i += 1
                    continue

        elif in_short:
            # 放空停損:盤中最高價突破防守線(最近3日高點),用被突破的價位出場
            if short_stop_level is not None and h > short_stop_level:
                _close_trade(trades, ticker, short_entry_date, short_entry_price, d, short_stop_level,
                             "放空停損(盤中突破近期高點)", side="short")
                in_short = False
                i += 1
                continue

            # 放空回補(v5改動):追蹤創新低,收盤突破「創新低那天前一根K棒高點」就回補
            if short_lowest_close is not None and c < short_lowest_close:
                short_lowest_close = c
                if i >= 1:
                    short_trailing_level = high.iloc[i - 1]

            if short_trailing_level is not None and c > short_trailing_level:
                _close_trade(trades, ticker, short_entry_date, short_entry_price, d, c,
                             "放空回補(突破前一根K棒高點)", side="short")
                in_short = False
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
                    setup_window_high = None
                    setup_window_low = None
                    setup_days_count = 0
                    i += 1
                    continue

                setup_window_low = min(setup_window_low, l)
                setup_days_count += 1
                if setup_days_count >= MAX_SETUP_DAYS:
                    pending_setup = False
                    setup_window_high = None
                    setup_window_low = None
                    setup_days_count = 0

            if not pending_setup and elig:
                touched_or_broke = (not pd.isna(m15) and l <= m15) or (not pd.isna(m43) and l <= m43)
                if touched_or_broke:
                    pending_setup = True
                    setup_window_high = h
                    setup_window_low = l
                    setup_days_count = 1

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


def run_backtest_v5(max_stocks: int | None = None):
    print("抓取股票清單...")
    universe = get_universe(include_otc=True)
    if max_stocks:
        universe = universe[:max_stocks]
    print(f"共 {len(universe)} 檔,開始回測(v5:v4基礎+放空回補改成突破前K高點)...")

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
                trades = simulate_trades_v5(df, t)
                all_trades.extend(trades)
            except Exception as e:
                print(f"[warn] {t} 回測失敗: {e}")
        time.sleep(BATCH_SLEEP)

    print(f"\n產生 {len(all_trades)} 筆交易,開始查鉅額交易紀錄(僅能涵蓋今年的交易)...")
    twse_codes_this_year = set(
        t["ticker"].replace(".TWO", "").replace(".TW", "")
        for t in all_trades
        if date.fromisoformat(t["entry_date"]).year == date.today().year
        and t["ticker"].endswith(".TW")
    )
    block_trade_cache = {}
    for code in twse_codes_this_year:
        block_trade_cache[code] = fetch_block_trade_dates(code)
        time.sleep(0.5)
    tag_block_trade(all_trades, block_trade_cache)

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


def print_stats_v5(trades: list[dict]):
    print("\n" + "=" * 60)
    print("回測結果統計 v5(v4基礎 + 放空回補改成突破前一根K棒高點)")
    print("=" * 60)

    print("\n【與前幾次結果對照】")
    print("  v3(時間停損改3天,無反手):           2346筆, 勝率43.9%, 期望值+1.88%")
    print("  v4(碰均線回補):                      2815筆, 勝率46.0%, 期望值+1.63%")
    print("    -- 其中放空單536筆, 勝率55.6%, 期望值+0.45%")

    long_trades = [t for t in trades if t.get("side", "long") == "long"]
    short_trades = [t for t in trades if t.get("side") == "short"]

    _stats_for(trades, "v5 全部交易(多單+空單)")
    _stats_for(long_trades, "v5 多單")
    _stats_for(short_trades, "v5 反手放空單")

    yes_group = [t for t in trades if t.get("block_trade_group") == "yes"]
    no_group = [t for t in trades if t.get("block_trade_group") == "no"]
    unknown_group = [t for t in trades if t.get("block_trade_group") == "unknown"]

    print(f"\n--- 鉅額交易分組比較(僅今年交易可分組,共{len(yes_group)+len(no_group)}筆;"
          f"另有{len(unknown_group)}筆因年份太早無法判斷)---")
    _stats_for(yes_group, "v5 近3個月有鉅額交易紀錄")
    _stats_for(no_group, "v5 近3個月無鉅額交易紀錄")

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
              f"出場原因={t.get('exit_reason')}, 鉅額交易={t.get('block_trade_group')}")

    print(f"\n報酬率最差的10筆交易:")
    for t in sorted(trades, key=lambda x: x["return_pct"])[:10]:
        print(f"  [{t.get('side','long')}] {t['ticker']}: {t['entry_date']}進場 → {t['exit_date']}出場, "
              f"報酬 {t['return_pct']:+.2f}%, 持有{t['holding_days']}天, "
              f"出場原因={t.get('exit_reason')}, 鉅額交易={t.get('block_trade_group')}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-stocks", type=int, default=None)
    args = parser.parse_args()

    trades = run_backtest_v5(max_stocks=args.max_stocks)
    print_stats_v5(trades)
