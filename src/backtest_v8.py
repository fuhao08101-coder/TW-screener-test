"""
回測工具 v8:結合「乖離背離訊號」進場 + 「碰均線回補」出場(v5驗證過表現最好的出場方式)。
這個組合還沒測試過,理論上是目前為止放空策略裡最有機會成功的版本。

篩選/進場訊號(跟 short_screener.py 完全一致):
  近25個交易日內,今天收盤價創這段期間新高,但今天的乖離比這段期間內
  「某個更早、乖離更高」的日子還要小(代表股價創高但均線已追上,動能減弱)
  且 ATR14絕對值 >= 10
  當天收盤價直接放空進場(訊號當天即進場,不用等額外確認)

停損:
  用「進場前3個交易日的最高點」當停損線(固定),收盤漲回這條線之上就停損回補

出場(獲利了結):
  股價(最低價)碰到15MA或43MA,在碰到的均線價位回補

還原日K:使用 yfinance auto_adjust=True。放空報酬率計算方式:
  (進場價 - 出場價) / 進場價 * 100,股價下跌才是獲利。
"""
from __future__ import annotations
import sys
import os
import time
from datetime import date
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from universe import get_universe
from backtest import _fetch_batch, _calc_atr, fetch_block_trade_dates, tag_block_trade, BATCH_SIZE, BATCH_SLEEP

# ------- V8 策略參數 -------
HISTORY_PERIOD = "5y"          # 依使用者要求,涵蓋2022逆風期
DIVERGENCE_LOOKBACK_DAYS = 25
BIAS_MA_PERIOD = 15
BIAS_MIN_THRESHOLD = 10.0
ATR_PERIOD = 14
ATR_MIN_THRESHOLD = 10.0
MA_SHORT = 15
MA_LONG = 43
STOPLOSS_LOOKBACK_DAYS = 3
SANITY_MAX_RETURN_PCT = 80.0   # 單筆報酬率超過這個絕對值,視為資料錯誤,不計入
SANITY_MAX_DAILY_JUMP = 3.0    # 單日股價變動超過這個倍數,視為資料異常,整檔跳過


def compute_divergence_signal(df: pd.DataFrame) -> pd.Series:
    """逐日判斷是否符合「乖離背離」訊號,回傳布林值序列"""
    close = df["Close"].dropna()
    ma15 = close.rolling(BIAS_MA_PERIOD).mean()
    bias = (close - ma15) / ma15 * 100.0
    atr = _calc_atr(df, ATR_PERIOD)

    signal = pd.Series(False, index=close.index)

    for i in range(DIVERGENCE_LOOKBACK_DAYS + BIAS_MA_PERIOD, len(close)):
        latest_close = close.iloc[i]
        latest_bias = bias.iloc[i]
        latest_atr = atr.iloc[i]

        if pd.isna(latest_bias) or latest_bias < BIAS_MIN_THRESHOLD:
            continue
        if pd.isna(latest_atr) or latest_atr < ATR_MIN_THRESHOLD:
            continue

        window_close = close.iloc[i - DIVERGENCE_LOOKBACK_DAYS + 1:i + 1]
        window_bias = bias.iloc[i - DIVERGENCE_LOOKBACK_DAYS + 1:i + 1]

        if latest_close < window_close.max() - 1e-6:
            continue

        prior_bias = window_bias.iloc[:-1].dropna()
        if prior_bias.empty:
            continue
        prior_peak_bias = prior_bias.max()
        prior_peak_date = prior_bias.idxmax()
        prior_peak_price = close.loc[prior_peak_date]

        if latest_bias >= prior_peak_bias:
            continue
        if latest_close <= prior_peak_price:
            continue

        signal.iloc[i] = True

    return signal


def simulate_v8_trades(df: pd.DataFrame, ticker: str) -> list[dict]:
    close = df["Close"].dropna()
    high = df["High"]
    low = df["Low"]

    min_len = max(MA_LONG, ATR_PERIOD, DIVERGENCE_LOOKBACK_DAYS, STOPLOSS_LOOKBACK_DAYS) + 30
    if len(close) < min_len:
        return []

    # 資料合理性檢查
    daily_ratio = close / close.shift(1)
    if ((daily_ratio > SANITY_MAX_DAILY_JUMP) | (daily_ratio < 1 / SANITY_MAX_DAILY_JUMP)).any():
        print(f"[warn] {ticker} 股價資料出現異常單日跳動,判斷是資料錯誤,整檔跳過不回測")
        return []

    ma15 = close.rolling(MA_SHORT).mean()
    ma43 = close.rolling(MA_LONG).mean()
    signal = compute_divergence_signal(df)

    trades = []
    dates = close.index

    in_position = False
    entry_price = None
    entry_date = None
    stop_loss_level = None

    i = min_len
    while i < len(dates):
        d = dates[i]
        c = close.iloc[i]
        l = low.iloc[i]
        m15 = ma15.iloc[i]
        m43 = ma43.iloc[i]

        if in_position:
            if stop_loss_level is not None and c > stop_loss_level:
                ret_pct = (entry_price - c) / entry_price * 100.0
                if abs(ret_pct) <= SANITY_MAX_RETURN_PCT:
                    trades.append({
                        "ticker": ticker,
                        "entry_date": entry_date.strftime("%Y-%m-%d"),
                        "exit_date": d.strftime("%Y-%m-%d"),
                        "entry_price": round(float(entry_price), 2),
                        "exit_price": round(float(c), 2),
                        "return_pct": round(float(ret_pct), 2),
                        "holding_days": (d - entry_date).days,
                        "exit_reason": "停損",
                    })
                in_position = False
                i += 1
                continue

            touched_15 = not pd.isna(m15) and l <= m15
            touched_43 = not pd.isna(m43) and l <= m43
            if touched_15 or touched_43:
                exit_price = m15 if touched_15 else m43
                ret_pct = (entry_price - exit_price) / entry_price * 100.0
                if abs(ret_pct) <= SANITY_MAX_RETURN_PCT:
                    trades.append({
                        "ticker": ticker,
                        "entry_date": entry_date.strftime("%Y-%m-%d"),
                        "exit_date": d.strftime("%Y-%m-%d"),
                        "entry_price": round(float(entry_price), 2),
                        "exit_price": round(float(exit_price), 2),
                        "return_pct": round(float(ret_pct), 2),
                        "holding_days": (d - entry_date).days,
                        "exit_reason": "獲利了結(碰均線回補)",
                    })
                in_position = False
                i += 1
                continue

        else:
            if bool(signal.iloc[i]):
                in_position = True
                entry_price = c
                entry_date = d
                lookback_start = max(0, i - STOPLOSS_LOOKBACK_DAYS)
                stop_loss_level = high.iloc[lookback_start:i].max()

        i += 1

    return trades


def run_backtest_v8(max_stocks: int | None = None):
    print("抓取股票清單...")
    universe = get_universe(include_otc=True)
    if max_stocks:
        universe = universe[:max_stocks]
    print(f"共 {len(universe)} 檔,開始回測(v8乖離背離+碰均線回補)...")

    import yfinance as yf

    def fetch_batch_5y(tickers):
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

    all_tickers = [row["ticker"] for row in universe]
    batches = [all_tickers[i:i + BATCH_SIZE] for i in range(0, len(all_tickers), BATCH_SIZE)]

    all_trades = []
    done = 0
    for batch_idx, batch in enumerate(batches, 1):
        print(f"批次 {batch_idx}/{len(batches)}(已處理 {done}/{len(all_tickers)} 檔)")
        batch_data = fetch_batch_5y(batch)
        for t in batch:
            done += 1
            df = batch_data.get(t)
            if df is None:
                continue
            try:
                trades = simulate_v8_trades(df, t)
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
    if avg_holding > 0:
        print(f"  年化周轉率參考: 252天 / {avg_holding:.1f}天 ≈ 每年可做 {252/avg_holding:.1f} 輪")


def print_stats_v8(trades: list[dict]):
    print("\n" + "=" * 60)
    print("回測結果統計 v8(乖離背離進場 + 碰均線回補出場,5年資料)")
    print("=" * 60)
    print("\n【與前面放空版本對照】")
    print("  v5(破底進場+碰均線回補):        3495筆, 勝率57.6%, 期望值-0.01%")
    print("  v6(V2鏡像框架):                 表現較差(負期望值)")
    print("  v7(背離訊號+V2框架):            期望值-0.51%~-0.66%")

    _stats_for(trades, "v8 全部交易")

    yes_group = [t for t in trades if t.get("block_trade_group") == "yes"]
    no_group = [t for t in trades if t.get("block_trade_group") == "no"]
    unknown_group = [t for t in trades if t.get("block_trade_group") == "unknown"]

    print(f"\n--- 鉅額交易分組比較(僅今年交易可分組,共{len(yes_group)+len(no_group)}筆;"
          f"另有{len(unknown_group)}筆因年份太早無法判斷)---")
    _stats_for(yes_group, "v8 近3個月有鉅額交易紀錄")
    _stats_for(no_group, "v8 近3個月無鉅額交易紀錄")

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

    trades = run_backtest_v8(max_stocks=args.max_stocks)
    print_stats_v8(trades)
