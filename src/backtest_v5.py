"""
回測工具 v5:放空策略,跟前面幾版(v1/v2/v3)的做多邏輯完全獨立、不共用篩選條件。

篩選(候選訊號日):
  ATR14絕對值 >= 10(股票夠活潑)
  且 當天15MA乖離 >= 10%(股價急漲過熱)

進場(放空):
  訊號日之後,逐日檢查,只要某天「盤中最低價」跌破前一天的最低點(逐日滾動比較),
  當下視為放空進場,進場價用「被跌破的價位」(前一天最低點)

停損:
  用「進場前3個交易日的最高點」當停損線(固定,進場當下就決定,不會之後再更新)
  進場後,只要收盤價漲回這條線之上,當天停損回補

出場(獲利了結):
  進場後,只要某天股價(最低價)碰到15MA或43MA,當天在碰到的均線價位回補
  (兩條都碰到的話,以先碰到的15MA為準,因為通常離現價較近)

還原日K:使用 yfinance auto_adjust=True。放空的報酬率計算方式與做多相反:
  (進場價 - 出場價) / 進場價 * 100,股價下跌才是獲利。
"""
from __future__ import annotations
import sys
import os
import time
from datetime import date
import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(__file__))
from universe import get_universe
from backtest import _fetch_batch, _calc_atr, fetch_block_trade_dates, tag_block_trade, HISTORY_PERIOD, BATCH_SIZE, BATCH_SLEEP

# ------- V5 策略參數 -------
MA_SHORT = 15
MA_LONG = 43
ATR_PERIOD = 14
BIAS_MA_PERIOD = 15

ATR_MIN_THRESHOLD_V5 = 10.0   # 篩選條件:ATR14絕對值門檻
BIAS_MIN_THRESHOLD_V5 = 10.0  # 篩選條件:15MA乖離門檻(%)

STOPLOSS_LOOKBACK_DAYS = 3    # 停損:進場前幾天的最高點
MAX_SETUP_DAYS = 20           # 訊號後最多等幾天沒破底,就放棄這個訊號


def compute_eligible_v5(df: pd.DataFrame) -> pd.Series:
    """V5專用的篩選邏輯,跟主要篩選器(compute_eligible_mask)是不同的、更簡單的條件"""
    close = df["Close"].dropna()
    ma15 = close.rolling(BIAS_MA_PERIOD).mean()
    bias = (close - ma15) / ma15 * 100.0
    atr = _calc_atr(df, ATR_PERIOD)

    eligible = (bias >= BIAS_MIN_THRESHOLD_V5) & (atr >= ATR_MIN_THRESHOLD_V5)
    return eligible.fillna(False)


def simulate_short_trades(df: pd.DataFrame, ticker: str) -> list[dict]:
    close = df["Close"].dropna()
    high = df["High"]
    low = df["Low"]

    min_len = max(MA_LONG, ATR_PERIOD, STOPLOSS_LOOKBACK_DAYS) + 20
    if len(close) < min_len:
        return []

    ma15 = close.rolling(MA_SHORT).mean()
    ma43 = close.rolling(MA_LONG).mean()
    eligible = compute_eligible_v5(df)

    trades = []
    dates = close.index

    in_position = False
    entry_price = None
    entry_date = None
    stop_loss_level = None

    pending_short = False
    setup_days_count = 0

    i = min_len
    while i < len(dates):
        d = dates[i]
        c = close.iloc[i]
        h = high.iloc[i]
        l = low.iloc[i]
        m15 = ma15.iloc[i]
        m43 = ma43.iloc[i]

        if in_position:
            # 停損:收盤漲回停損線之上
            if stop_loss_level is not None and c > stop_loss_level:
                ret_pct = (entry_price - c) / entry_price * 100.0
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

            # 出場:碰到15MA或43MA就回補
            touched_15 = not pd.isna(m15) and l <= m15
            touched_43 = not pd.isna(m43) and l <= m43
            if touched_15 or touched_43:
                exit_price = m15 if touched_15 else m43
                ret_pct = (entry_price - exit_price) / entry_price * 100.0
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
            if pending_short:
                # 破底確認:當天最低價跌破前一天最低點
                if i >= 1 and l < low.iloc[i - 1]:
                    in_position = True
                    entry_price = low.iloc[i - 1]  # 被跌破的價位
                    entry_date = d
                    lookback_start = max(0, i - STOPLOSS_LOOKBACK_DAYS)
                    stop_loss_level = high.iloc[lookback_start:i].max()  # 進場前3天最高點(固定)
                    pending_short = False
                    setup_days_count = 0
                    i += 1
                    continue

                setup_days_count += 1
                if setup_days_count >= MAX_SETUP_DAYS:
                    pending_short = False
                    setup_days_count = 0

            if not pending_short and bool(eligible.iloc[i]):
                pending_short = True
                setup_days_count = 0

        i += 1

    return trades


def run_backtest_v5(max_stocks: int | None = None):
    print("抓取股票清單...")
    universe = get_universe(include_otc=True)
    if max_stocks:
        universe = universe[:max_stocks]
    print(f"共 {len(universe)} 檔,開始回測(v5放空策略)...")

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
                trades = simulate_short_trades(df, t)
                all_trades.extend(trades)
            except Exception as e:
                print(f"[warn] {t} 回測失敗: {e}")
        time.sleep(BATCH_SLEEP)

    print(f"\n產生 {len(all_trades)} 筆放空交易,開始查鉅額交易紀錄(僅能涵蓋今年的交易)...")
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
    print(f"  年化周轉率參考: 252天 / {avg_holding:.1f}天 ≈ 每年可做 {252/avg_holding:.1f} 輪(單一部位、無縫接軌情況下)")


def print_stats_v5(trades: list[dict]):
    print("\n" + "=" * 60)
    print("回測結果統計 v5(放空策略,ATR14>=10 + 15MA乖離>=10%)")
    print("=" * 60)

    _stats_for(trades, "v5 全部交易")

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

    trades = run_backtest_v5(max_stocks=args.max_stocks)
    print_stats_v5(trades)
