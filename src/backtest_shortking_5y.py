"""
短線王(組合C,已定案版本)5年回測,直接跟V2用同樣的資料期間比較,才是公平對照。

進場條件(跟現行 shortking_screener.py 完全一致):
  ATR14絕對值 >= 8、收盤 > 15MA、15MA乖離 >= 8%、收盤 > 近5個交易日最高點

出場規則(沿用V10-C框架,跟之前所有組合C相關測試一致):
  停損:盤中跌破訊號日低點 / 收盤跌破15MA
  停利:進場滿6個交易日未達+3%直接出場 / 達標後連續2天未創新高 /
        或創高後跌破前一根K棒低點(動態防守線)

資料範圍:近5年(跟V2的5年逆風版回測用同樣的期間,才能公平比較)。

還原日K:使用 yfinance auto_adjust=True。
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


def _close_trade(trades, ticker, market, entry_date, entry_price, exit_date, exit_price, reason):
    ret_pct = (exit_price - entry_price) / entry_price * 100.0
    if abs(ret_pct) > SANITY_MAX_RETURN_PCT:
        return
    holding_days = (exit_date - entry_date).days
    trades.append({
        "ticker": ticker, "market": market,
        "entry_date": entry_date.strftime("%Y-%m-%d"),
        "exit_date": exit_date.strftime("%Y-%m-%d"),
        "return_pct": round(float(ret_pct), 2),
        "holding_days": holding_days,
        "exit_reason": reason,
    })


def simulate_stock(df: pd.DataFrame, ticker: str, market: str) -> list[dict]:
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

    entry_signal = (
        (close > ma15) & (atr >= ATR_MIN_THRESHOLD) &
        (bias >= BIAS_MIN_THRESHOLD) & (close > recent_high)
    ).fillna(False)

    trades = []
    dates = close.index

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
                _close_trade(trades, ticker, market, entry_date, entry_price, d, signal_low, "停損(訊號日低點)")
                in_position = False
                i += 1
                continue
            if not pd.isna(m15) and c < m15:
                _close_trade(trades, ticker, market, entry_date, entry_price, d, c, "停損(跌破15MA)")
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
                        _close_trade(trades, ticker, market, entry_date, entry_price, d, c, "時間停損(未達3%)")
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
                    _close_trade(trades, ticker, market, entry_date, entry_price, d, c, "停利(連續2天未創高)")
                    in_position = False
                    i += 1
                    continue
                if l < trailing_low_level:
                    _close_trade(trades, ticker, market, entry_date, entry_price, d, c, "停利(跌破前K棒)")
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

    return trades


def run_backtest(max_stocks: int | None = None):
    print("抓取股票清單...")
    universe = get_universe(include_otc=True)
    if max_stocks:
        universe = universe[:max_stocks]
    print(f"共 {len(universe)} 檔,開始回測(短線王組合C,5年,無雙買)...")

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
                trades = simulate_stock(df, t, ticker_market.get(t, "?"))
                all_trades.extend(trades)
            except Exception as e:
                print(f"[warn] {t} 回測失敗: {e}")
        time.sleep(BATCH_SLEEP)

    print(f"\n產生 {len(all_trades)} 筆交易。")
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
    print("短線王(組合C,無雙買)5年回測 vs V2(大撈家2.0核心)5年回測")
    print("=" * 70)
    print("\n【V2對照組(已知結果,5年含2022逆風期)】")
    print("  1974筆, 勝率41.7%, 平均獲利+14.50%, 平均虧損-6.56%, 期望值+2.23%, 平均持有9.2天")

    _stats_for(trades, "短線王(組合C,5年)全部")

    twse = [t for t in trades if t["market"] == "TWSE"]
    tpex = [t for t in trades if t["market"] == "TPEX"]
    print()
    _stats_for(twse, "短線王(組合C,5年)/ 上市")
    _stats_for(tpex, "短線王(組合C,5年)/ 上櫃")

    print(f"\n--- 出場原因分布 ---")
    reason_counts = {}
    for t in trades:
        r = t.get("exit_reason", "未知")
        reason_counts[r] = reason_counts.get(r, 0) + 1
    for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
        pct = count / len(trades) * 100 if trades else 0
        avg_ret = sum(t["return_pct"] for t in trades if t.get("exit_reason") == reason) / count
        print(f"  {reason}: {count}筆({pct:.1f}%), 平均報酬 {avg_ret:+.2f}%")

    print("\n" + "=" * 70)
    print(f"\n報酬率最好的10筆交易:")
    for t in sorted(trades, key=lambda x: x["return_pct"], reverse=True)[:10]:
        print(f"  {t['ticker']}({t['market']}): {t['entry_date']}進場 → {t['exit_date']}出場, "
              f"報酬 {t['return_pct']:+.2f}%, 持有{t['holding_days']}天, 出場原因={t.get('exit_reason')}")

    print(f"\n報酬率最差的10筆交易:")
    for t in sorted(trades, key=lambda x: x["return_pct"])[:10]:
        print(f"  {t['ticker']}({t['market']}): {t['entry_date']}進場 → {t['exit_date']}出場, "
              f"報酬 {t['return_pct']:+.2f}%, 持有{t['holding_days']}天, 出場原因={t.get('exit_reason')}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-stocks", type=int, default=None)
    args = parser.parse_args()

    trades = run_backtest(max_stocks=args.max_stocks)
    print_report(trades)
