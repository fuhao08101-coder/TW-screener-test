"""
回測工具 v3:跟 v2 唯一的差別是「發動後的停利觸發方式」,其他規則完全相同。
用來單獨測試「跌破前一根K棒低點」VS「跌破15MA」哪一種停利效果更好。

【重複使用 backtest.py 的部分】:
  - 「歷史上是否符合正式版完整篩選條件」的逐日判斷(compute_eligible_mask)
  - 鉅額交易分組邏輯(fetch_block_trade_dates / tag_block_trade)
  - 批次下載機制(_fetch_batch)

進場:
  1. 候選訊號日:當天符合完整篩選條件,且股價碰到或跌破15MA/43MA(最低價 <= 均線)
     開始等待進場的這段期間,持續累積「整理期最低點」(給停損用,見下方)
  2. 訊號日之後,逐日檢查,只要某天「盤中最高價」突破「前一天的最高點」(逐日滾動比較,
     不是比整段整理期的最高點),當下視為進場,進場價用「被突破的價位」(前一天最高點)

停損:
  用「從碰到均線那天開始,一路到進場前累積的整理期最低點」(區間底部)當停損線,
  不是只看進場那天自己的低點。進場後,只要收盤價跌破這條線,當天停損出場。

出場(兩階段):
  階段1(時間停損):進場後持有滿6個交易日,檢查未實現獲利有沒有達到+3%,
                  沒有的話,第6個交易日收盤直接賣出
  階段2(獲利啟動後的停利機制,僅在階段1存活且獲利>=3%後才啟動):
    - 持續追蹤「進場後最高收盤價」,只要連續3個交易日沒有創新高,出場
    - 【v3改動】或者收盤跌破15MA,出場(取代v2的「跌破前一根K棒低點」)
    - 兩者哪個先發生,就用哪個出場

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

HOLD_DAYS_CHECKPOINT = 6      # 第一階段:持有幾個交易日後檢查獲利門檻
PROFIT_THRESHOLD_PCT = 3.0    # 第一階段:獲利門檻(%)
STALL_DAYS_LIMIT = 3          # 第二階段:連續幾天沒創新高就出場


def simulate_trades_v3(df: pd.DataFrame, ticker: str) -> list[dict]:
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

    in_position = False
    entry_price = None
    entry_date = None
    entry_idx = None
    stop_loss_level = None

    # 第二階段追蹤用的狀態
    activated = False           # 是否已經進入「獲利啟動後」的停利邏輯
    highest_close = None
    days_since_new_high = 0
    # (v3不需要trailing_stop_level,改用15MA直接判斷)

    pending_setup = False       # 是否正在等待「突破整理期高點」的進場確認
    setup_window_high = None    # 整理期累積的最高點(進場突破的比較基準)
    setup_window_low = None     # 整理期累積的最低點(停損線的來源)
    setup_days_count = 0        # 整理期已經持續幾天(避免無限期等待)
    MAX_SETUP_DAYS = 20         # 整理期超過這麼多天還沒突破,就放棄這個訊號重新找

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
            # 停損:收盤跌破「整理期累積的最低點」(不是進場當天自己的低點)
            if stop_loss_level is not None and c < stop_loss_level:
                _close_trade(trades, ticker, entry_date, entry_price, d, c, "停損")
                in_position = False
                activated = False
                i += 1
                continue

            holding_days = i - entry_idx  # 進場當天算第0天

            if not activated:
                # 第一階段:第6個交易日檢查獲利門檻
                if holding_days >= HOLD_DAYS_CHECKPOINT:
                    ret_pct = (c - entry_price) / entry_price * 100.0
                    if ret_pct < PROFIT_THRESHOLD_PCT:
                        _close_trade(trades, ticker, entry_date, entry_price, d, c, "時間停損(未達3%)")
                        in_position = False
                        i += 1
                        continue
                    else:
                        # 達標,啟動第二階段
                        activated = True
                        highest_close = c
                        days_since_new_high = 0
            else:
                # 第二階段(v3版本):追蹤創新高 / 連續未創高 / 跌破15MA
                if c > highest_close:
                    highest_close = c
                    days_since_new_high = 0
                else:
                    days_since_new_high += 1

                # v3改動:用「收盤跌破15MA」取代「跌破前一根K棒低點」當停利觸發
                if not pd.isna(m15) and c < m15:
                    _close_trade(trades, ticker, entry_date, entry_price, d, c, "停利(跌破15MA)")
                    in_position = False
                    activated = False
                    i += 1
                    continue

                if days_since_new_high >= STALL_DAYS_LIMIT:
                    _close_trade(trades, ticker, entry_date, entry_price, d, c, "停利(連續3天未創新高)")
                    in_position = False
                    activated = False
                    i += 1
                    continue

        else:
            if pending_setup:
                # 進場突破:只要「當天最高價」超過「前一天的最高點」就進場(逐日滾動比較,
                # 不是比整段整理期的最高點)。停損線則是用「整段整理期累積的最低點」
                # (區間底部),這兩個基準不一樣,分開處理。
                if i >= 1 and h > high.iloc[i - 1]:
                    in_position = True
                    entry_price = high.iloc[i - 1]  # 突破的價位(前一天高點)
                    entry_date = d
                    entry_idx = i
                    stop_loss_level = setup_window_low  # 整理期累積的最低點(區間底部)當停損線
                    pending_setup = False
                    setup_window_high = None
                    setup_window_low = None
                    setup_days_count = 0
                    i += 1
                    continue

                # 還沒突破,把今天的低點併入整理期累積範圍(持續加寬防守區間)
                setup_window_low = min(setup_window_low, l)
                setup_days_count += 1

                if setup_days_count >= MAX_SETUP_DAYS:
                    # 整理太久還沒突破,放棄這個訊號,回到尋找新訊號的狀態
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


def run_backtest_v3(max_stocks: int | None = None):
    print("抓取股票清單...")
    universe = get_universe(include_otc=True)
    if max_stocks:
        universe = universe[:max_stocks]
    print(f"共 {len(universe)} 檔,開始回測(v2進出場邏輯)...")

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
                trades = simulate_trades_v3(df, t)
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


def print_stats_v3(trades: list[dict]):
    print("\n" + "=" * 60)
    print("回測結果統計 v3(發動後改用跌破15MA停利,對比v2的跌破前1根K棒)")
    print("=" * 60)
    print("\n【與前幾次結果對照】")
    print("  近2年版(簡單15MA跌破出場):        723筆, 勝率32.4%, 期望值+3.13%")
    print("  近5年版(含2022逆風期):             1343筆, 勝率31.3%, 期望值+2.17%")
    print("  v2(整理期停損+跌破前2根K停利):     1947筆, 勝率40.9%, 期望值+2.13%")
    print("  v2(整理期停損+跌破前1根K停利):     1974筆, 勝率41.7%, 期望值+2.23%")
    print("  v3(整理期停損+發動後跌破15MA停利): 這次結果 ↓")

    _stats_for(trades, "v2 全部交易")

    yes_group = [t for t in trades if t.get("block_trade_group") == "yes"]
    no_group = [t for t in trades if t.get("block_trade_group") == "no"]
    unknown_group = [t for t in trades if t.get("block_trade_group") == "unknown"]

    print(f"\n--- 鉅額交易分組比較(僅今年交易可分組,共{len(yes_group)+len(no_group)}筆;"
          f"另有{len(unknown_group)}筆因年份太早無法判斷)---")
    _stats_for(yes_group, "v2 近3個月有鉅額交易紀錄")
    _stats_for(no_group, "v2 近3個月無鉅額交易紀錄")

    # 出場原因分布,方便你了解交易大多是怎麼結束的
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
              f"出場原因={t.get('exit_reason')}, 鉅額交易={t.get('block_trade_group')}")

    print(f"\n報酬率最差的10筆交易:")
    for t in sorted(trades, key=lambda x: x["return_pct"])[:10]:
        print(f"  {t['ticker']}: {t['entry_date']}進場 → {t['exit_date']}出場, "
              f"報酬 {t['return_pct']:+.2f}%, 持有{t['holding_days']}天, "
              f"出場原因={t.get('exit_reason')}, 鉅額交易={t.get('block_trade_group')}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-stocks", type=int, default=None)
    args = parser.parse_args()

    trades = run_backtest_v3(max_stocks=args.max_stocks)
    print_stats_v3(trades)
