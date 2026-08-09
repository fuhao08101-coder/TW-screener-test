"""
回測工具:驗證「碰到15MA或43MA進場、跌破15MA出場」這套策略,過去5年在全市場的表現
(涵蓋2022年逆風期,測試策略在非多頭環境下是否還站得住腳)。
不是每天自動跑的功能,是你想確認策略有沒有用時,手動觸發執行的獨立工具。

【重要修正】這次回測只在「歷史上真正符合正式版完整篩選條件」的日子之後,
才允許尋找15/43MA進場訊號,不是隨便哪天碰到均線都算——
也就是模擬「只交易正式版掃描器歷史上真的會抓到的股票」。

篩選條件(跟 screener.py 完全一致):
  近30日內15MA乖離 >= 20%、收盤 > SMA87、近15日未破87MA、
  SMA87 > SMA284、ATR14絕對值>=9 且 佔股價>=1.5%

策略規則:
  進場候選訊號:某天股價區間(最低~最高)有涵蓋到15MA或43MA,且當天符合完整篩選條件
  確認進場:訊號日之後,只要某天收盤價「突破訊號日的最高點」,當天收盤價進場
  出場:進場之後,只要某天收盤價跌破15MA,當天收盤價出場

鉅額交易分組(有限制):資料源只能查到「今年」的鉅額交易紀錄,所以只有今年發生的交易
能被正確分類成「有/無鉅額交易」,更早年份的交易會標記為「無法判斷」,不會亂猜。

還原日K:使用 yfinance auto_adjust=True。
"""
from __future__ import annotations
import sys
import os
import time
from datetime import date, datetime, timedelta
import pandas as pd
import yfinance as yf
import requests

sys.path.insert(0, os.path.dirname(__file__))
from universe import get_universe

# ------- 策略參數(進出場) -------
MA_SHORT = 15
MA_LONG = 43
HISTORY_PERIOD = "5y"
BATCH_SIZE = 150
BATCH_SLEEP = 1.0

# ------- 篩選條件參數(與 screener.py 完全一致) -------
BIAS_LOOKBACK_DAYS = 30
BIAS_MA_PERIOD = 15
BIAS_THRESHOLD = 20.0
LONG_MA_PERIOD = 87
MA87_BREACH_LOOKBACK = 15
SECOND_MA_PERIOD = 284
ATR_PERIOD = 14
ATR_MIN_THRESHOLD = 9.0
ATR_MIN_PCT_THRESHOLD = 1.5

# ------- 鉅額交易查詢 -------
BLOCK_TRADE_URL = "https://www.twse.com.tw/rwd/zh/block/BFIAUU_sd"
BLOCK_TRADE_LOOKBACK_DAYS = 90

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


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


def _calc_atr(df: pd.DataFrame, period: int) -> pd.Series:
    high = df["High"]
    low = df["Low"]
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        (high - low), (high - prev_close).abs(), (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def compute_eligible_mask(df: pd.DataFrame) -> pd.Series:
    """對整段歷史,逐日判斷「當天是否符合正式版完整篩選條件」,回傳布林值序列"""
    close = df["Close"].dropna()
    high = df["High"]
    low = df["Low"]

    ma15 = close.rolling(BIAS_MA_PERIOD).mean()
    ma87 = close.rolling(LONG_MA_PERIOD).mean()
    ma284 = close.rolling(SECOND_MA_PERIOD).mean()
    bias = (close - ma15) / ma15 * 100.0
    atr = _calc_atr(df, ATR_PERIOD)
    atr_pct = atr / close * 100.0

    bias_window_max = bias.rolling(BIAS_LOOKBACK_DAYS).max()
    cond1 = bias_window_max >= BIAS_THRESHOLD
    cond2 = close > ma87

    breach = close < ma87
    no_breach_recent = ~breach.rolling(MA87_BREACH_LOOKBACK).max().astype(bool)
    cond3 = no_breach_recent

    cond4 = ma87 > ma284
    cond5 = (atr >= ATR_MIN_THRESHOLD) & (atr_pct >= ATR_MIN_PCT_THRESHOLD)

    eligible = cond1 & cond2 & cond3 & cond4 & cond5
    return eligible.fillna(False)


def simulate_trades(df: pd.DataFrame) -> list[dict]:
    """只在 eligible=True 的日子才允許尋找進場訊號,其餘邏輯跟策略規則一致"""
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
    in_position = False
    entry_price = None
    entry_date = None
    pending_setup_high = None

    dates = close.index
    for i in range(min_len, len(dates)):
        d = dates[i]
        c = close.iloc[i]
        h = high.iloc[i]
        l = low.iloc[i]
        m15 = ma15.iloc[i]
        m43 = ma43.iloc[i]
        elig = bool(eligible.iloc[i]) if i < len(eligible) else False

        if in_position:
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
            if pending_setup_high is not None:
                if c > pending_setup_high:
                    in_position = True
                    entry_price = c
                    entry_date = d
                    pending_setup_high = None
                    continue

            # 只有符合完整篩選條件的那天,才能當作候選訊號日
            if elig:
                touched_15 = not pd.isna(m15) and l <= m15 <= h
                touched_43 = not pd.isna(m43) and l <= m43 <= h
                if touched_15 or touched_43:
                    pending_setup_high = h

    return trades


def fetch_block_trade_dates(stock_code: str) -> set[str] | None:
    """查這檔股票「今年」的鉅額交易日期清單,回傳 None 代表查詢失敗(不是沒有,是查不到)"""
    today = date.today()
    params = {
        "response": "json",
        "startDate": today.strftime("%Y0101"),
        "endDate": today.strftime("%Y%m%d"),
        "stockNo": stock_code,
    }
    try:
        r = requests.get(BLOCK_TRADE_URL, headers=HEADERS, params=params, timeout=20)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:
        return None

    dates = set()
    for row in data.get("data", []):
        if not row or row[0] == "總計":
            continue
        try:
            d = datetime.strptime(row[0], "%Y/%m/%d").date()
            dates.add(d.isoformat())
        except (ValueError, IndexError):
            continue
    return dates


def tag_block_trade(trades: list[dict], block_trade_cache: dict[str, set[str] | None]) -> None:
    """幫每筆交易標記 block_trade_group: 'yes' / 'no' / 'unknown'(原地修改)"""
    current_year = date.today().year
    for t in trades:
        entry_dt = date.fromisoformat(t["entry_date"])
        code = t["ticker"].replace(".TWO", "").replace(".TW", "")

        if entry_dt.year != current_year:
            t["block_trade_group"] = "unknown"
            continue

        block_dates = block_trade_cache.get(code)
        if block_dates is None:
            t["block_trade_group"] = "unknown"
            continue

        cutoff = entry_dt - timedelta(days=BLOCK_TRADE_LOOKBACK_DAYS)
        has_block = any(cutoff.isoformat() <= bd <= entry_dt.isoformat() for bd in block_dates)
        t["block_trade_group"] = "yes" if has_block else "no"


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


def print_stats(trades: list[dict]):
    print("\n" + "=" * 60)
    print("回測結果統計(只計入「歷史上真正符合完整篩選條件」的交易)")
    print("=" * 60)

    _stats_for(trades, "全部交易")

    yes_group = [t for t in trades if t.get("block_trade_group") == "yes"]
    no_group = [t for t in trades if t.get("block_trade_group") == "no"]
    unknown_group = [t for t in trades if t.get("block_trade_group") == "unknown"]

    print(f"\n--- 鉅額交易分組比較(僅今年交易可分組,共{len(yes_group)+len(no_group)}筆;"
          f"另有{len(unknown_group)}筆因年份太早無法判斷)---")
    _stats_for(yes_group, "近3個月有鉅額交易紀錄")
    _stats_for(no_group, "近3個月無鉅額交易紀錄")

    print("\n" + "=" * 60)
    print(f"\n報酬率最好的10筆交易:")
    for t in sorted(trades, key=lambda x: x["return_pct"], reverse=True)[:10]:
        print(f"  {t['ticker']}: {t['entry_date']}進場 → {t['exit_date']}出場, "
              f"報酬 {t['return_pct']:+.2f}%, 持有{t['holding_days']}天, "
              f"鉅額交易={t.get('block_trade_group')}")

    print(f"\n報酬率最差的10筆交易:")
    for t in sorted(trades, key=lambda x: x["return_pct"])[:10]:
        print(f"  {t['ticker']}: {t['entry_date']}進場 → {t['exit_date']}出場, "
              f"報酬 {t['return_pct']:+.2f}%, 持有{t['holding_days']}天, "
              f"鉅額交易={t.get('block_trade_group')}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-stocks", type=int, default=None, help="限制測試股票數量(測試用)")
    args = parser.parse_args()

    trades = run_backtest(max_stocks=args.max_stocks)
    print_stats(trades)
