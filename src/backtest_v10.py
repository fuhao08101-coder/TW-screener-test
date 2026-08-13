"""
回測工具 v10:新的做多策略測試,跟目前最強的V2比較。

【進場】(當天同時符合,收盤價直接進場,沒有像V2那樣的「碰均線+隔天突破確認」流程)
- ATR14絕對值 >= ATR_MIN_THRESHOLD
- 收盤 > 15MA
- 15MA乖離(收盤相對15MA) >= BIAS_THRESHOLD_ENTRY

【停損】(任一觸發)
- 盤中最低價跌破「訊號當天的最低點」→ 用被跌破的價位(訊號日低點)出場
- 收盤價跌破當天15MA → 用收盤價出場

【停利】(任一觸發,單階段,沒有V2那種「先達+3%才啟動」的門檻)
- 連續3天沒有創收盤新高 → 用收盤價出場
- 盤中最低價跌破前一天的最低點 → 用收盤價出場

【本版暫不包含】
「外資融資雙買」條件——這需要另外抓歷史逐日的外資買賣超+融資增減資料,
資料量遠大於目前使用的yfinance價格資料,先用其他條件驗證策略主體,
如果方向可行,再另外把這個條件當濾網疊加上去。

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
STALL_DAYS_LIMIT = 3


def simulate_trades_v10(df: pd.DataFrame, ticker: str) -> list[dict]:
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

    in_position = False
    entry_price = None
    entry_date = None
    signal_low = None       # 訊號當天的最低點(停損用)
    highest_close = None
    days_since_new_high = 0
    trailing_low_level = None   # 創高那天前一根K棒的低點(動態防守線,取代固定的昨天低點)

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
                             "停損(跌破訊號日低點)")
                in_position = False
                i += 1
                continue

            # 停損2:收盤跌破15MA
            if not pd.isna(m15) and c < m15:
                _close_trade(trades, ticker, entry_date, entry_price, d, c,
                             "停損(收盤跌破15MA)")
                in_position = False
                i += 1
                continue

            # 停利1:連續3天未創收盤新高,同時更新動態防守線
            if c > highest_close:
                highest_close = c
                days_since_new_high = 0
                if i >= 1:
                    trailing_low_level = low.iloc[i - 1]  # 創高那天前一根K棒的低點
            else:
                days_since_new_high += 1

            if days_since_new_high >= STALL_DAYS_LIMIT:
                _close_trade(trades, ticker, entry_date, entry_price, d, c,
                             "停利(連續3天未創新高)")
                in_position = False
                i += 1
                continue

            # 停利2:盤中跌破「創高那天前一根K棒的低點」(動態防守線,還沒創過高就沒有這條線)
            if trailing_low_level is not None and l < trailing_low_level:
                _close_trade(trades, ticker, entry_date, entry_price, d, c,
                             "停利(跌破創高前一根K棒低點)")
                in_position = False
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


def run_backtest_v10(max_stocks: int | None = None):
    print("抓取股票清單...")
    universe = get_universe(include_otc=True)
    if max_stocks:
        universe = universe[:max_stocks]
    print(f"共 {len(universe)} 檔,開始回測(v10:新做多策略測試)...")

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
                trades = simulate_trades_v10(df, t)
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


def print_stats_v10(trades: list[dict]):
    print("\n" + "=" * 60)
    print("回測結果統計 v10(新做多策略,未含外資融資雙買條件)")
    print("=" * 60)

    print("\n【與V2對照】")
    print("  V2(目前最強): 1974筆, 勝率41.7%, 期望值+2.23%")

    _stats_for(trades, "v10 全部交易")

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

    trades = run_backtest_v10(max_stocks=args.max_stocks)
    print_stats_v10(trades)
