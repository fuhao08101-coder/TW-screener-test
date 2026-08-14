"""
回測工具 v12:V10-C核心價格條件(ATR14≥8、收盤>15MA、突破近5日高點)
+ 外資融資雙買濾網(僅上市TWSE,近9個交易日內雙買、之後未雙減),
出場沿用v10-C的規則。

因為外資/融資資料源沒辦法抓5年歷史(太慢、風險高),這次回測範圍縮小成
最近半年(約125個交易日),只回測上市(TWSE)股票——雙買濾網目前只支援上市,
上櫃排除在這次回測之外。

【進場】(當天同時符合,收盤價直接進場):
- ATR14絕對值 >= 8
- 收盤 > 15MA
- 盤中最高價突破近5個交易日高點(不含今天;用盤中價比對,同時涵蓋「盤中或收盤突破」)
- 當天在外資融資雙買合格名單內(近9個交易日內雙買過,且之後沒有雙減)

【出場】(沿用v10-C):
停損:盤中跌破訊號日低點 / 收盤跌破15MA
停利:連續2天未創收盤新高 / 盤中跌破動態防守線(創高那天前一根K棒的低點)
"""
from __future__ import annotations
import sys
import os
import time
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from universe import get_universe
from backtest import _fetch_batch, _calc_atr, BATCH_SIZE, BATCH_SLEEP
from institutional_flow_history import build_qualified_dates_by_code

SHORT_MA_PERIOD = 15
BREAKOUT_LOOKBACK_DAYS = 5
ATR_PERIOD = 14
ATR_MIN_THRESHOLD = 8.0
STALL_DAYS_LIMIT = 2
DUAL_BUY_WINDOW = 9
HISTORY_PERIOD = "1y"  # 只需要涵蓋半年回測期+均線計算的緩衝,不用抓5年


def simulate_trades_v12(df: pd.DataFrame, ticker: str, qualified_dates: set[str]) -> list[dict]:
    close = df["Close"].dropna()
    high = df["High"]
    low = df["Low"]

    min_len = max(SHORT_MA_PERIOD, ATR_PERIOD, BREAKOUT_LOOKBACK_DAYS) + 30
    if len(close) < min_len:
        return []

    ma15 = close.rolling(SHORT_MA_PERIOD).mean()
    atr14 = _calc_atr(df, ATR_PERIOD)
    recent_high = high.rolling(BREAKOUT_LOOKBACK_DAYS).max().shift(1)

    trades = []
    dates = close.index

    in_position = False
    entry_price = None
    entry_date = None
    signal_low = None
    highest_close = None
    days_since_new_high = 0
    trailing_low_level = None

    i = min_len
    while i < len(dates):
        d = dates[i]
        date_str = d.strftime("%Y%m%d")
        c = close.iloc[i]
        h = high.iloc[i]
        l = low.iloc[i]
        m15 = ma15.iloc[i]
        a = atr14.iloc[i]
        rh = recent_high.iloc[i]

        if in_position:
            if signal_low is not None and l < signal_low:
                _close_trade(trades, ticker, entry_date, entry_price, d, signal_low,
                             "停損(跌破訊號日低點)")
                in_position = False
                i += 1
                continue

            if not pd.isna(m15) and c < m15:
                _close_trade(trades, ticker, entry_date, entry_price, d, c,
                             "停損(收盤跌破15MA)")
                in_position = False
                i += 1
                continue

            if c > highest_close:
                highest_close = c
                days_since_new_high = 0
                if i >= 1:
                    trailing_low_level = low.iloc[i - 1]
            else:
                days_since_new_high += 1

            if days_since_new_high >= STALL_DAYS_LIMIT:
                _close_trade(trades, ticker, entry_date, entry_price, d, c,
                             "停利(連續2天未創新高)")
                in_position = False
                i += 1
                continue

            if trailing_low_level is not None and l < trailing_low_level:
                _close_trade(trades, ticker, entry_date, entry_price, d, c,
                             "停利(跌破創高前一根K棒低點)")
                in_position = False
                i += 1
                continue

        else:
            if (
                date_str in qualified_dates
                and not pd.isna(m15) and c > m15
                and not pd.isna(rh) and h > rh
                and not pd.isna(a) and a >= ATR_MIN_THRESHOLD
            ):
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


def run_backtest_v12(max_stocks: int | None = None):
    print("步驟1/2:抓取外資融資歷史資料、計算每日雙買合格名單(這步會花一些時間)...")
    qualified_dates_by_code = build_qualified_dates_by_code(window=DUAL_BUY_WINDOW)

    print("\n步驟2/2:抓股票清單(僅上市TWSE)...")
    universe = get_universe(include_otc=True)
    universe = [row for row in universe if row["market"] == "TWSE"]
    if max_stocks:
        universe = universe[:max_stocks]
    print(f"共 {len(universe)} 檔上市股票,開始回測(v12:短線王條件+外資融資雙買,半年)...")

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
            code = t.replace(".TW", "")
            qualified_dates = qualified_dates_by_code.get(code, set())
            if not qualified_dates:
                continue  # 這檔股票在整個回測期間從未通過雙買濾網,不用模擬
            try:
                trades = simulate_trades_v12(df, t, qualified_dates)
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


def print_stats_v12(trades: list[dict]):
    print("\n" + "=" * 60)
    print("回測結果統計 v12(短線王條件+外資融資雙買,近半年,僅上市)")
    print("=" * 60)

    print("\n【對照:v10-C(無雙買濾網,5年,含上市+上櫃)】")
    print("  3900筆, 勝率37.1%, 期望值+1.30%")

    _stats_for(trades, "v12 全部交易")

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

    trades = run_backtest_v12(max_stocks=args.max_stocks)
    print_stats_v12(trades)
