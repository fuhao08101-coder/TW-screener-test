"""
短線王(組合C+大盤濾網)疊加「每日僅前3高乖離進場」規則的對照回測。

比較兩組:
  A. 全部訊號進場:當天符合條件的股票,全部都進場(現行版本的做法)
  B. 每日僅前3高乖離進場:當天符合條件的股票裡,只挑「15MA乖離最高的前3名」進場,
     其餘符合條件但沒排進前3的,當天不進場(不管有幾檔符合,最多3檔)

這是為了驗證「乖離最高=動能最強=品質更好」這個假設,是否有回測數據支持。

【技術架構跟之前的回測不一樣】:之前的回測都是「每檔股票獨立模擬」,互不影響。
這次「前3名」的判斷需要「同一天、全市場股票互相比較排名」,所以改成:
  第一步:每檔股票各自算出entry_signal(進場訊號)、收盤、最低、15MA、乖離 的完整序列
  第二步:用共同的交易日曆(拿加權指數的日期當基準,台股各股票交易日基本一致),
         逐日走訪,每天先處理「已持有部位」的出場判斷,再處理「新訊號」的進場判斷
         (變體A:全部進場;變體B:當天所有候選按乖離排序,只留前3名進場)

資料範圍:近5年,大盤濾網用法跟之前驗證過的版本一致(^TWII / ^TWOII)。
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

TWSE_INDEX_TICKER = "^TWII"
OTC_INDEX_TICKER = "^TWOII"

TOP_N_PER_DAY = 3  # 每日最多幾檔新進場(變體B用)


def _calc_atr(df: pd.DataFrame, period: int) -> pd.Series:
    high = df["High"]
    low = df["Low"]
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        (high - low), (high - prev_close).abs(), (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def fetch_index_regime(ticker: str, max_retries: int = 3) -> dict:
    """
    回傳 {日期字串: True/False},True代表當天該指數收盤 > 指數自己的15MA。
    改用「分段抓取」:一次要5年份資料量對某些指數代號(尤其^TWOII)不穩定,
    容易整個失敗,改成每次只抓約1年,分5段抓完再拼起來,每段查詢範圍小,
    穩定性比較高,即使某一段失敗也只需要重試那一段,不用整個重來。
    """
    from datetime import date, timedelta

    end_date = date.today()
    total_days = 365 * 5 + 30
    chunk_days = 370  # 每段抓約1年多一點,確保有重疊、不漏資料

    chunks = []
    chunk_end = end_date
    while (end_date - chunk_end).days < total_days:
        chunk_start = chunk_end - timedelta(days=chunk_days)
        chunks.append((chunk_start, chunk_end))
        chunk_end = chunk_start
    chunks.reverse()  # 由舊到新排列,方便後續合併

    all_frames = []
    for seg_idx, (c_start, c_end) in enumerate(chunks, 1):
        df = None
        for attempt in range(1, max_retries + 1):
            print(f"抓取指數({ticker})第{seg_idx}/{len(chunks)}段"
                  f"({c_start}~{c_end})...(第{attempt}次嘗試)")
            try:
                df = yf.Ticker(ticker).history(start=c_start, end=c_end, auto_adjust=True)
            except Exception as e:
                print(f"[warn] 指數{ticker}第{seg_idx}段第{attempt}次抓取失敗: {e}")
                df = None
            if df is not None and not df.empty:
                break
            if attempt < max_retries:
                time.sleep(5)
        if df is not None and not df.empty:
            all_frames.append(df)
        else:
            print(f"[warn] 指數{ticker}第{seg_idx}段最終仍失敗,這段資料會缺漏")
        time.sleep(2)  # 每段之間留一點間隔

    if not all_frames:
        print(f"[warn] 指數{ticker}所有分段都抓取失敗,回傳空結果")
        return {}

    df_all = pd.concat(all_frames)
    df_all = df_all[~df_all.index.duplicated(keep="last")].sort_index()

    if len(df_all) < 200:
        print(f"[warn] 指數{ticker}合併後仍只有 {len(df_all)} 天,資料不足")
        return {}

    close = df_all["Close"].dropna()
    ma15 = close.rolling(SHORT_MA_PERIOD).mean()
    regime = (close > ma15).fillna(False)

    out = {}
    for dt, val in regime.items():
        out[dt.strftime("%Y-%m-%d")] = bool(val)
    print(f"指數{ticker}環境資料:共 {len(out)} 個交易日,其中站上15MA的天數: {sum(out.values())} 天")
    return out


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


def prepare_stock_series(df: pd.DataFrame, ticker: str, market: str, twse_regime: dict, otc_regime: dict):
    """回傳每檔股票的 {日期: {close, low, ma15, bias, entry_signal}} 字典,方便逐日查表"""
    close = df["Close"].dropna()
    high = df["High"]
    low = df["Low"]

    min_len = max(SHORT_MA_PERIOD, ATR_PERIOD, BREAKOUT_LOOKBACK_DAYS) + 60
    if len(close) < min_len:
        return None

    daily_ratio = close / close.shift(1)
    if ((daily_ratio > SANITY_MAX_DAILY_JUMP) | (daily_ratio < 1 / SANITY_MAX_DAILY_JUMP)).any():
        return None

    ma15 = close.rolling(SHORT_MA_PERIOD).mean()
    bias = (close - ma15) / ma15 * 100.0
    atr = _calc_atr(df, ATR_PERIOD)
    recent_high = high.rolling(BREAKOUT_LOOKBACK_DAYS).max().shift(1)

    base_signal = (
        (close > ma15) & (atr >= ATR_MIN_THRESHOLD) &
        (bias >= BIAS_MIN_THRESHOLD) & (close > recent_high)
    ).fillna(False)

    regime_map = twse_regime if market == "TWSE" else otc_regime

    result = {}
    for dt in close.index:
        date_key = dt.strftime("%Y-%m-%d")
        idx_ok = regime_map.get(date_key, False)
        signal = bool(base_signal.loc[dt]) and idx_ok
        result[date_key] = {
            "close": float(close.loc[dt]),
            "low": float(low.loc[dt]),
            "ma15": float(ma15.loc[dt]) if not pd.isna(ma15.loc[dt]) else None,
            "bias": float(bias.loc[dt]) if not pd.isna(bias.loc[dt]) else None,
            "entry_signal": signal,
        }
    return result


def run_coordinated_simulation(all_stock_data: dict, master_dates: list[str], variant: str) -> list[dict]:
    """
    all_stock_data: {ticker: {market, series: {date_str: {...}}}}
    variant: "全部訊號" 或 "前3名"
    """
    trades = []
    positions = {}  # ticker -> {entry_date, entry_price, signal_low, highest_close,
                     #            days_since_new_high, trailing_low_level, activated, entry_day_idx}

    for day_idx, date_key in enumerate(master_dates):
        # 第一步:處理所有已持有部位的出場判斷
        to_remove = []
        for ticker, pos in positions.items():
            info = all_stock_data[ticker]["series"].get(date_key)
            if info is None:
                continue
            c = info["close"]
            l = info["low"]
            m15 = info["ma15"]

            if l < pos["signal_low"]:
                _record_trade(trades, ticker, all_stock_data[ticker]["market"], variant,
                               pos, date_key, pos["signal_low"], "停損(訊號日低點)")
                to_remove.append(ticker)
                continue
            if m15 is not None and c < m15:
                _record_trade(trades, ticker, all_stock_data[ticker]["market"], variant,
                               pos, date_key, c, "停損(跌破15MA)")
                to_remove.append(ticker)
                continue

            holding_days = day_idx - pos["entry_day_idx"]

            if not pos["activated"]:
                if c > pos["highest_close"]:
                    pos["highest_close"] = c
                if holding_days >= HOLD_DAYS_CHECKPOINT:
                    ret_now = (c - pos["entry_price"]) / pos["entry_price"] * 100.0
                    if ret_now < PROFIT_THRESHOLD_PCT:
                        _record_trade(trades, ticker, all_stock_data[ticker]["market"], variant,
                                       pos, date_key, c, "時間停損(未達3%)")
                        to_remove.append(ticker)
                        continue
                    else:
                        pos["activated"] = True
                        pos["highest_close"] = c
                        pos["days_since_new_high"] = 0
                        prev_idx = day_idx - 1
                        if prev_idx >= 0:
                            prev_info = all_stock_data[ticker]["series"].get(master_dates[prev_idx])
                            pos["trailing_low_level"] = prev_info["low"] if prev_info else None
            else:
                if c > pos["highest_close"]:
                    pos["highest_close"] = c
                    pos["days_since_new_high"] = 0
                    prev_idx = day_idx - 1
                    if prev_idx >= 0:
                        prev_info = all_stock_data[ticker]["series"].get(master_dates[prev_idx])
                        pos["trailing_low_level"] = prev_info["low"] if prev_info else None
                else:
                    pos["days_since_new_high"] += 1

                if pos["days_since_new_high"] >= STALL_DAYS_LIMIT:
                    _record_trade(trades, ticker, all_stock_data[ticker]["market"], variant,
                                   pos, date_key, c, "停利(連續2天未創高)")
                    to_remove.append(ticker)
                    continue
                if pos["trailing_low_level"] is not None and l < pos["trailing_low_level"]:
                    _record_trade(trades, ticker, all_stock_data[ticker]["market"], variant,
                                   pos, date_key, c, "停利(跌破前K棒)")
                    to_remove.append(ticker)
                    continue

        for t in to_remove:
            del positions[t]

        # 第二步:找出當天新訊號候選
        candidates = []
        for ticker, data in all_stock_data.items():
            if ticker in positions:
                continue
            info = data["series"].get(date_key)
            if info is None or not info["entry_signal"]:
                continue
            candidates.append((ticker, info["bias"] or 0, info["close"], info["low"]))

        if variant == "前3名":
            candidates.sort(key=lambda x: x[1], reverse=True)
            candidates = candidates[:TOP_N_PER_DAY]

        for ticker, bias_val, c, l in candidates:
            positions[ticker] = {
                "entry_date": date_key,
                "entry_day_idx": day_idx,
                "entry_price": c,
                "signal_low": l,
                "highest_close": c,
                "days_since_new_high": 0,
                "trailing_low_level": None,
                "activated": False,
            }

    return trades


def _record_trade(trades, ticker, market, variant, pos, exit_date_key, exit_price, reason):
    entry_price = pos["entry_price"]
    ret_pct = (exit_price - entry_price) / entry_price * 100.0
    if abs(ret_pct) > SANITY_MAX_RETURN_PCT:
        return
    from datetime import date as _date
    entry_d = _date.fromisoformat(pos["entry_date"])
    exit_d = _date.fromisoformat(exit_date_key)
    holding_days = (exit_d - entry_d).days
    trades.append({
        "ticker": ticker, "market": market, "variant": variant,
        "entry_date": pos["entry_date"], "exit_date": exit_date_key,
        "return_pct": round(ret_pct, 2), "holding_days": holding_days,
        "exit_reason": reason,
    })


def run_backtest(max_stocks: int | None = None):
    twse_regime = fetch_index_regime(TWSE_INDEX_TICKER)
    print("等待10秒再抓下一個指數,避免連續請求觸發API限流...")
    time.sleep(10)
    otc_regime = fetch_index_regime(OTC_INDEX_TICKER)

    if len(twse_regime) < 200 or len(otc_regime) < 200:
        print("\n❌ 指數資料抓取不完整,提早中止。")
        return []

    print("抓取股票清單...")
    universe = get_universe(include_otc=True)
    if max_stocks:
        universe = universe[:max_stocks]
    print(f"共 {len(universe)} 檔,開始準備每檔股票的訊號序列...")

    all_tickers = [row["ticker"] for row in universe]
    ticker_market = {row["ticker"]: row["market"] for row in universe}
    batches = [all_tickers[i:i + BATCH_SIZE] for i in range(0, len(all_tickers), BATCH_SIZE)]

    all_stock_data = {}
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
                series = prepare_stock_series(df, t, ticker_market.get(t, "?"), twse_regime, otc_regime)
                if series:
                    all_stock_data[t] = {"market": ticker_market.get(t, "?"), "series": series}
            except Exception as e:
                print(f"[warn] {t} 準備資料失敗: {e}")
        time.sleep(BATCH_SLEEP)

    print(f"\n共 {len(all_stock_data)} 檔股票準備完成,開始逐日模擬...")

    # 用共同交易日曆(取所有股票日期的聯集,由舊到新排序)
    all_dates = set()
    for data in all_stock_data.values():
        all_dates.update(data["series"].keys())
    master_dates = sorted(all_dates)
    print(f"共同交易日曆:{len(master_dates)} 天")

    print("模擬變體A:全部訊號進場...")
    trades_all = run_coordinated_simulation(all_stock_data, master_dates, "全部訊號")
    print(f"產生 {len(trades_all)} 筆交易")

    print("模擬變體B:每日僅前3高乖離進場...")
    trades_top3 = run_coordinated_simulation(all_stock_data, master_dates, "前3名")
    print(f"產生 {len(trades_top3)} 筆交易")

    return trades_all + trades_top3


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
    print("全部訊號進場 vs 每日僅前3高乖離進場")
    print("=" * 70)

    for v in ["全部訊號", "前3名"]:
        v_trades = [t for t in trades if t["variant"] == v]
        print(f"\n--- {v} ---")
        _stats_for(v_trades, f"{v} / 全部")
        twse = [t for t in v_trades if t["market"] == "TWSE"]
        tpex = [t for t in v_trades if t["market"] == "TPEX"]
        _stats_for(twse, f"{v} / 上市")
        _stats_for(tpex, f"{v} / 上櫃")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-stocks", type=int, default=None)
    args = parser.parse_args()

    trades = run_backtest(max_stocks=args.max_stocks)
    print_report(trades)
