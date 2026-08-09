"""
回測工具:驗證「碰到15MA或43MA進場、跌破15MA出場」這套策略,過去2年在全市場的表現。
不是每天自動跑的功能,是你想確認策略有沒有用時,手動觸發執行的獨立工具。

策略規則:
  進場候選訊號:某天股價區間(最低~最高)有涵蓋到15MA或43MA(代表回測到支撐)
  確認進場:訊號日之後,只要某天收盤價「突破訊號日的最高點」,當天收盤價進場
           (不是碰到當下衝進去,要等突破確認,比較保守)
  出場:進場之後,只要某天收盤價跌破15MA,當天收盤價出場
       (同一條規則,同時扮演停利/停損兩種角色:先漲後跌破=停利,沒漲就跌破=停損)
  同一檔股票同一時間只會有一筆交易在進行,出場後才會繼續找下一個訊號

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

MA_SHORT = 15
MA_LONG = 43
HISTORY_PERIOD = "2y"
BATCH_SIZE = 150
BATCH_SLEEP = 1.0

HEADERS_NOTE = "使用與screener.py相同的批次下載機制"


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


def simulate_trades(df: pd.DataFrame) -> list[dict]:
    """對單一股票跑一次策略模擬,回傳這檔股票產生的所有交易紀錄"""
    close = df["Close"].dropna()
    high = df["High"]
    low = df["Low"]

    min_len = max(MA_SHORT, MA_LONG) + 10
    if len(close) < min_len:
        return []

    ma15 = close.rolling(MA_SHORT).mean()
    ma43 = close.rolling(MA_LONG).mean()

    trades = []
    in_position = False
    entry_price = None
    entry_date = None
    pending_setup_high = None  # 等待突破確認的訊號日高點
    pending_setup_date = None

    dates = close.index
    for i in range(min_len, len(dates)):
        d = dates[i]
        c = close.iloc[i]
        h = high.iloc[i]
        l = low.iloc[i]
        m15 = ma15.iloc[i]
        m43 = ma43.iloc[i]

        if in_position:
            # 出場條件:收盤跌破15MA
            if not pd.isna(m15) and c < m15:
                ret_pct = (c - entry_price) / entry_price * 100.0
                holding_days = (d - entry_date).days
                trades.append({
                    "entry_date": entry_date.strftime("%Y-%m-%d"),
                    "exit_date": d.strftime("%Y-%m-%d"),
                    "entry_price": round(float(entry_price), 2),
                    "exit_price": round(float(c), 2),
                    "return_pct": round(float(ret_pct), 2),
                    "holding_days": holding_days,
                })
                in_position = False
                entry_price = None
                entry_date = None
        else:
            # 等待突破確認
            if pending_setup_high is not None:
                if c > pending_setup_high:
                    in_position = True
                    entry_price = c
                    entry_date = d
                    pending_setup_high = None
                    pending_setup_date = None
                    continue

            # 尋找新的候選訊號日(碰到15MA或43MA)
            touched_15 = not pd.isna(m15) and l <= m15 <= h
            touched_43 = not pd.isna(m43) and l <= m43 <= h
            if touched_15 or touched_43:
                pending_setup_high = h
                pending_setup_date = d

    return trades


def run_backtest(max_stocks: int | None = None):
    print("抓取股票清單...")
    universe = get_universe(include_otc=True)
    if max_stocks:
        universe = universe[:max_stocks]
    print(f"共 {len(universe)} 檔,開始回測...")

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
                trades = simulate_trades(df)
                for tr in trades:
                    tr["ticker"] = t
                all_trades.extend(trades)
            except Exception as e:
                print(f"[warn] {t} 回測失敗: {e}")
        time.sleep(BATCH_SLEEP)

    return all_trades


def print_stats(trades: list[dict]):
    if not trades:
        print("沒有產生任何交易紀錄。")
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

    print("\n" + "=" * 50)
    print("回測結果統計")
    print("=" * 50)
    print(f"總交易筆數: {total}")
    print(f"勝率: {win_rate:.1f}%({len(wins)}勝 / {len(losses)}敗)")
    print(f"平均報酬率(每筆交易): {avg_return:.2f}%")
    print(f"平均獲利(賺錢的交易): +{avg_win:.2f}%")
    print(f"平均虧損(賠錢的交易): {avg_loss:.2f}%")
    print(f"期望值(每筆交易平均賺賠): {expectancy:.2f}%")
    print(f"平均持有天數: {avg_holding:.1f} 天")
    print("=" * 50)

    print(f"\n報酬率最好的10筆交易:")
    for t in sorted(trades, key=lambda x: x["return_pct"], reverse=True)[:10]:
        print(f"  {t['ticker']}: {t['entry_date']}進場 → {t['exit_date']}出場, "
              f"報酬 {t['return_pct']:+.2f}%, 持有{t['holding_days']}天")

    print(f"\n報酬率最差的10筆交易:")
    for t in sorted(trades, key=lambda x: x["return_pct"])[:10]:
        print(f"  {t['ticker']}: {t['entry_date']}進場 → {t['exit_date']}出場, "
              f"報酬 {t['return_pct']:+.2f}%, 持有{t['holding_days']}天")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-stocks", type=int, default=None, help="限制測試股票數量(測試用)")
    args = parser.parse_args()

    trades = run_backtest(max_stocks=args.max_stocks)
    print_stats(trades)
