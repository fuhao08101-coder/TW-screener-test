"""
短線王(大撈家短線版)回測:同時比較4種進場規則組合,共用同一份下載資料、同一套出場邏輯。

【本次回測範圍限制】外資融資雙買濾網無法納入回測(資料源只能查「現在」往前9天,
沒有歷史每日資料),這次只驗證價格邏輯層面:突破方式、乖離門檻。

4種進場組合:
  A. 盤中突破3日高 + 乖離8%以上(目前版本)
  B. 盤中突破3日高 + 無乖離限制
  C. 收盤站上5日高 + 乖離8%以上
  D. 收盤站上5日高 + 無乖離限制
共同條件:ATR14絕對值 >= 8、收盤 > 15MA

出場規則(4種組合共用同一套,沿用V10-C框架):
  停損:盤中跌破訊號日低點 / 收盤跌破15MA
  停利:進場滿6個交易日未達+3%直接出場 / 達標後連續2天未創新高 /
        或創高後跌破前一根K棒低點(動態防守線)

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

HISTORY_PERIOD = "2y"
BATCH_SIZE = 150
BATCH_SLEEP = 1.0

ATR_PERIOD = 14
ATR_MIN_THRESHOLD = 8.0
SHORT_MA_PERIOD = 15
BIAS_MIN_THRESHOLD = 8.0

BREAKOUT_INTRADAY_DAYS = 3   # 方案A/B:盤中突破3日高
BREAKOUT_CLOSE_DAYS = 5      # 方案C/D:收盤站上5日高

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


def simulate_variant(close, high, low, ma15, bias, atr, entry_signal, ticker, market, variant, trades):
    """給定一組「進場訊號」布林序列,跑一次完整的停損停利模擬,把交易紀錄append進trades"""
    dates = close.index
    min_len = 100  # 前面已經確保過整體資料夠長,這裡只是保險

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

            if holding_days < HOLD_DAYS_CHECKPOINT or highest_close is None:
                if c > (highest_close if highest_close is not None else -1e18):
                    highest_close = c
            if holding_days >= HOLD_DAYS_CHECKPOINT:
                ret_pct_now = (c - entry_price) / entry_price * 100.0
                if trailing_low_level is None and ret_pct_now < PROFIT_THRESHOLD_PCT:
                    _close_trade(trades, ticker, market, variant, entry_date, entry_price, d, c, "時間停損")
                    in_position = False
                    i += 1
                    continue
                elif trailing_low_level is None:
                    highest_close = c
                    days_since_new_high = 0
                    if i >= 1:
                        trailing_low_level = low.iloc[i - 1]

            if trailing_low_level is not None:
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


def simulate_all_variants(df: pd.DataFrame, ticker: str, market: str) -> list[dict]:
    close = df["Close"].dropna()
    high = df["High"]
    low = df["Low"]

    min_len = max(SHORT_MA_PERIOD, ATR_PERIOD, BREAKOUT_INTRADAY_DAYS, BREAKOUT_CLOSE_DAYS) + 100
    if len(close) < min_len:
        return []

    daily_ratio = close / close.shift(1)
    if ((daily_ratio > SANITY_MAX_DAILY_JUMP) | (daily_ratio < 1 / SANITY_MAX_DAILY_JUMP)).any():
        return []

    ma15 = close.rolling(SHORT_MA_PERIOD).mean()
    bias = (close - ma15) / ma15 * 100.0
    atr = _calc_atr(df, ATR_PERIOD)

    base_cond = (close > ma15) & (atr >= ATR_MIN_THRESHOLD)

    recent_high_intraday = high.rolling(BREAKOUT_INTRADAY_DAYS).max().shift(1)
    breakout_A = high > recent_high_intraday

    recent_high_close = high.rolling(BREAKOUT_CLOSE_DAYS).max().shift(1)
    breakout_C = close > recent_high_close

    bias_ok = bias >= BIAS_MIN_THRESHOLD

    signal_A = (base_cond & breakout_A & bias_ok).fillna(False)
    signal_B = (base_cond & breakout_A).fillna(False)
    signal_C = (base_cond & breakout_C & bias_ok).fillna(False)
    signal_D = (base_cond & breakout_C).fillna(False)

    trades = []
    simulate_variant(close, high, low, ma15, bias, atr, signal_A, ticker, market, "A_intraday3+bias8", trades)
    simulate_variant(close, high, low, ma15, bias, atr, signal_B, ticker, market, "B_intraday3+nobias", trades)
    simulate_variant(close, high, low, ma15, bias, atr, signal_C, ticker, market, "C_close5+bias8", trades)
    simulate_variant(close, high, low, ma15, bias, atr, signal_D, ticker, market, "D_close5+nobias", trades)
    return trades


def run_backtest(max_stocks: int | None = None):
    print("抓取股票清單...")
    universe = get_universe(include_otc=True)
    if max_stocks:
        universe = universe[:max_stocks]
    print(f"共 {len(universe)} 檔,開始回測(4組合同時比較)...")

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
                trades = simulate_all_variants(df, t, ticker_market.get(t, "?"))
                all_trades.extend(trades)
            except Exception as e:
                print(f"[warn] {t} 回測失敗: {e}")
        time.sleep(BATCH_SLEEP)

    print(f"\n產生 {len(all_trades)} 筆交易(4種組合合計)。")
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
    variants = ["A_intraday3+bias8", "B_intraday3+nobias", "C_close5+bias8", "D_close5+nobias"]

    print("\n" + "=" * 70)
    print("問題2+3:四種進場組合整體對照(尚未拆市場)")
    print("=" * 70)
    for v in variants:
        v_trades = [t for t in trades if t["variant"] == v]
        _stats_for(v_trades, v)

    print("\n" + "=" * 70)
    print("問題1:上市 vs 上櫃,依組合分別統計")
    print("=" * 70)
    for v in variants:
        print(f"\n--- {v} ---")
        v_trades = [t for t in trades if t["variant"] == v]
        twse_trades = [t for t in v_trades if t["market"] == "TWSE"]
        tpex_trades = [t for t in v_trades if t["market"] == "TPEX"]
        _stats_for(twse_trades, f"{v} / 上市TWSE")
        _stats_for(tpex_trades, f"{v} / 上櫃TPEX")

    print("\n" + "=" * 70)
    print("結論摘要,方便快速比對")
    print("=" * 70)
    print("突破方式比較(A+B平均 vs C+D平均,固定乖離條件相同時比較):")
    for pair_name, v1, v2 in [("有乖離8%限制", "A_intraday3+bias8", "C_close5+bias8"),
                                ("無乖離限制", "B_intraday3+nobias", "D_close5+nobias")]:
        t1 = [t for t in trades if t["variant"] == v1]
        t2 = [t for t in trades if t["variant"] == v2]
        print(f"\n[{pair_name}] 盤中突破3日高 vs 收盤站上5日高:")
        _stats_for(t1, f"盤中突破3日高({v1})")
        _stats_for(t2, f"收盤站上5日高({v2})")

    print("\n乖離門檻比較(固定突破方式相同時比較):")
    for pair_name, v1, v2 in [("盤中突破3日高", "A_intraday3+bias8", "B_intraday3+nobias"),
                                ("收盤站上5日高", "C_close5+bias8", "D_close5+nobias")]:
        t1 = [t for t in trades if t["variant"] == v1]
        t2 = [t for t in trades if t["variant"] == v2]
        print(f"\n[{pair_name}] 乖離8%以上 vs 無乖離限制:")
        _stats_for(t1, f"乖離8%以上({v1})")
        _stats_for(t2, f"無乖離限制({v2})")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-stocks", type=int, default=None)
    args = parser.parse_args()

    trades = run_backtest(max_stocks=args.max_stocks)
    print_report(trades)
