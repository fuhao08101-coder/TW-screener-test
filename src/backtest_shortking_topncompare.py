"""
每日候選數量(TOP_N)三方比較回測:
  A. 每日只取前3名
  B. 每日取前6名(現行版本)
  C. 每日取前10名

其他條件完全不變:ATR14>=8、收盤>15MA、收盤>近5日高點、大盤環境濾網、
外資融資雙買(9日內)、乖離門檻固定用已定案的5%。

用途:資金體量變大之後,是否應該同時開更多檔位分散風險?這支回測用來
比較「分散檔數」對整體期望值、勝率、單筆品質的影響。

用法: python src/backtest_shortking_topncompare.py --start 2025-08-01 --end 2026-08-31 --max-stocks 100
"""
from __future__ import annotations
import sys
import os
import time
import argparse
from datetime import date, timedelta, datetime
import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(__file__))
from universe import get_universe
from institutional_flow import fetch_day_flow

ATR_PERIOD = 14
ATR_MIN_THRESHOLD = 8.0
SHORT_MA_PERIOD = 15
BREAKOUT_LOOKBACK_DAYS = 5

HOLD_DAYS_CHECKPOINT = 6
PROFIT_THRESHOLD_PCT = 3.0
STALL_DAYS_LIMIT = 2

SANITY_MAX_RETURN_PCT = 80.0
SANITY_MAX_DAILY_JUMP = 3.0

DUAL_BUY_LOOKBACK_DAYS = 9
TOP_N_PER_DAY = 6

BATCH_SIZE = 150
BATCH_SLEEP = 1.0

FIXED_BIAS_THRESHOLD = 5.0  # 固定用已定案的5%乖離門檻,只測試TOP_N的影響

TOPN_VARIANTS = {
    "A_前3名": 3,
    "B_前6名": 6,
    "C_前10名": 10,
}



def _calc_atr(df: pd.DataFrame, period: int) -> pd.Series:
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    if isinstance(high, pd.DataFrame):
        high = high.iloc[:, 0]
    if isinstance(low, pd.DataFrame):
        low = low.iloc[:, 0]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low), (high - prev_close).abs(), (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def fetch_twii_regime(start_date: date, end_date: date, max_retries: int = 3) -> dict:
    df = None
    for attempt in range(1, max_retries + 1):
        print(f"抓取指數(^TWII)...(第{attempt}次嘗試)")
        try:
            df = yf.Ticker("^TWII").history(
                start=start_date - timedelta(days=60), end=end_date, auto_adjust=True)
        except Exception as e:
            print(f"[warn] ^TWII第{attempt}次抓取失敗: {e}")
            df = None
        if df is not None and not df.empty and len(df) >= SHORT_MA_PERIOD + 5:
            break
        if attempt < max_retries:
            time.sleep(5)
    if df is None or df.empty:
        print("[warn] ^TWII資料不足,回傳空結果")
        return {}
    close = df["Close"].dropna()
    ma15 = close.rolling(SHORT_MA_PERIOD).mean()
    regime = (close > ma15).fillna(False)
    out = {dt.strftime("%Y-%m-%d"): bool(v) for dt, v in regime.items()}
    print(f"^TWII環境資料:共 {len(out)} 個交易日")
    return out


def fetch_otc_regime_official(start_date: date, end_date: date) -> dict:
    import requests
    url = "https://www.tpex.org.tw/web/stock/iNdex_info/inxh/inx_result.php"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }

    all_rows = []
    cursor = date(end_date.year, end_date.month, 1)
    stop_before = date(start_date.year, start_date.month, 1) - timedelta(days=60)

    while cursor >= stop_before:
        roc_year = cursor.year - 1911
        date_param = f"{roc_year}/{cursor.month:02d}"
        for attempt in range(1, 4):
            try:
                r = requests.get(url, headers=headers, params={"l": "zh-tw", "d": date_param}, timeout=20)
                if r.status_code == 200:
                    payload = r.json()
                    tables = payload.get("tables") or []
                    if tables:
                        fields = tables[0].get("fields") or []
                        rows = tables[0].get("data") or []
                        idx_date = fields.index("日期") if "日期" in fields else 0
                        idx_close = fields.index("收市") if "收市" in fields else 4
                        for row in rows:
                            if len(row) <= max(idx_date, idx_close):
                                continue
                            date_str = row[idx_date].replace("/", "-")
                            try:
                                close_val = float(row[idx_close])
                            except (ValueError, TypeError):
                                continue
                            all_rows.append((date_str, close_val))
                    break
            except Exception as e:
                print(f"[warn] 櫃買指數 {date_param} 第{attempt}次失敗: {e}")
            time.sleep(1)
        year, month = cursor.year, cursor.month
        month -= 1
        if month == 0:
            month = 12
            year -= 1
        cursor = date(year, month, 1)
        time.sleep(0.3)

    if len(all_rows) < SHORT_MA_PERIOD + 5:
        print("[warn] 櫃買指數官方報表資料不足")
        return {}

    all_rows.sort(key=lambda x: x[0])
    closes = pd.Series([c for _, c in all_rows], index=pd.to_datetime([d for d, _ in all_rows]))
    closes = closes[~closes.index.duplicated(keep="last")].sort_index()
    ma15 = closes.rolling(SHORT_MA_PERIOD).mean()
    regime = (closes > ma15).fillna(False)
    out = {dt.strftime("%Y-%m-%d"): bool(v) for dt, v in regime.items()}
    print(f"櫃買指數環境資料:共 {len(out)} 個交易日")
    return out


def fetch_dualbuy_history(start_date: date, end_date: date) -> list:
    print(f"開始抓取{start_date}~{end_date}的外資融資歷史資料(每天全市場一次)...")
    collected = []
    d = start_date
    total = 0
    while d <= end_date:
        foreign_map, margin_map = fetch_day_flow(d)
        if foreign_map and margin_map:
            collected.append((d, foreign_map, margin_map))
        total += 1
        if total % 30 == 0:
            print(f"  已嘗試 {total} 天,成功取得 {len(collected)} 個交易日的資料...")
        d += timedelta(days=1)
    print(f"外資融資歷史資料抓取完成,共 {len(collected)} 個有效交易日")
    return collected


def build_dualbuy_qualified_by_day(flow_history: list) -> dict:
    qualified_by_day = {}
    for i, (d, _, _) in enumerate(flow_history):
        date_key = d.strftime("%Y-%m-%d")
        window = flow_history[max(0, i - DUAL_BUY_LOOKBACK_DAYS + 1):i + 1]
        window_codes = set()
        for _, fm, mm in window:
            window_codes.update(fm.keys())
            window_codes.update(mm.keys())

        qualified = set()
        for code in window_codes:
            trigger_idx = None
            for idx, (_, fm, mm) in enumerate(window):
                if fm.get(code, 0) > 0 and mm.get(code, 0) > 0:
                    trigger_idx = idx
                    break
            if trigger_idx is None:
                continue
            dual_sell_after = False
            for idx in range(trigger_idx, len(window)):
                _, fm, mm = window[idx]
                if fm.get(code, 0) < 0 and mm.get(code, 0) < 0:
                    dual_sell_after = True
                    break
            if not dual_sell_after:
                qualified.add(code)
        qualified_by_day[date_key] = qualified
    return qualified_by_day


def _fetch_batch(tickers: list[str], period: str) -> dict[str, pd.DataFrame]:
    out = {}
    try:
        data = yf.download(
            tickers=tickers, period=period, auto_adjust=True,
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


def prepare_stock_series(df: pd.DataFrame, market: str, twse_regime: dict, otc_regime: dict,
                          start_date: date, end_date: date):
    """回傳每天的 {close, low, ma15, bias, base_ok(不含乖離門檻的其餘條件是否通過)}"""
    close = df["Close"]
    high = df["High"]
    low = df["Low"]

    # 防護:yfinance不同版本,批次下載偶爾會讓欄位變成多層DataFrame而不是單純的
    # 一維Series,導致後續比較運算出錯("Can only compare identically-labeled
    # Series objects")。這裡強制squeeze成一維,並且用.values重新包裝、拿掉可能
    # 造成索引不一致的多餘欄位資訊,確保後面的運算穩定。
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    if isinstance(high, pd.DataFrame):
        high = high.iloc[:, 0]
    if isinstance(low, pd.DataFrame):
        low = low.iloc[:, 0]

    close = close.dropna()
    high = high.reindex(close.index)
    low = low.reindex(close.index)

    min_len = max(SHORT_MA_PERIOD, ATR_PERIOD, BREAKOUT_LOOKBACK_DAYS) + 30
    if len(close) < min_len:
        return None

    daily_ratio = close / close.shift(1)
    if ((daily_ratio > SANITY_MAX_DAILY_JUMP) | (daily_ratio < 1 / SANITY_MAX_DAILY_JUMP)).any():
        return None

    ma15 = close.rolling(SHORT_MA_PERIOD).mean()
    bias = (close - ma15) / ma15 * 100.0
    atr = _calc_atr(df, ATR_PERIOD)
    recent_high = high.rolling(BREAKOUT_LOOKBACK_DAYS).max().shift(1)

    # 不含乖離的其餘條件(給三個變體共用)
    base_cond = (
        (close > ma15) & (atr >= ATR_MIN_THRESHOLD) & (close > recent_high)
    ).fillna(False)

    regime_map = twse_regime if market == "TWSE" else otc_regime

    result = {}
    for dt in close.index:
        d_only = dt.date()
        if d_only < start_date or d_only > end_date:
            continue
        date_key = dt.strftime("%Y-%m-%d")
        idx_ok = regime_map.get(date_key, False)
        base_ok = bool(base_cond.loc[dt]) and idx_ok
        result[date_key] = {
            "close": float(close.loc[dt]),
            "low": float(low.loc[dt]),
            "ma15": float(ma15.loc[dt]) if not pd.isna(ma15.loc[dt]) else None,
            "bias": float(bias.loc[dt]) if not pd.isna(bias.loc[dt]) else None,
            "base_ok": base_ok,
        }
    return result if result else None


def run_coordinated_simulation(all_stock_data: dict, master_dates: list[str], dualbuy_by_day: dict,
                                top_n: int, variant_name: str) -> list[dict]:
    trades = []
    positions = {}

    for day_idx, date_key in enumerate(master_dates):
        to_remove = []
        for ticker, pos in positions.items():
            info = all_stock_data[ticker]["series"].get(date_key)
            if info is None:
                continue
            c, l, m15 = info["close"], info["low"], info["ma15"]

            if l < pos["signal_low"]:
                _record_trade(trades, ticker, all_stock_data[ticker]["market"], variant_name, pos,
                               date_key, pos["signal_low"], "停損(訊號日低點)")
                to_remove.append(ticker); continue
            if m15 is not None and c < m15:
                _record_trade(trades, ticker, all_stock_data[ticker]["market"], variant_name, pos,
                               date_key, c, "停損(跌破15MA)")
                to_remove.append(ticker); continue

            holding_days = day_idx - pos["entry_day_idx"]
            if not pos["activated"]:
                if c > pos["highest_close"]:
                    pos["highest_close"] = c
                if holding_days >= HOLD_DAYS_CHECKPOINT:
                    ret_now = (c - pos["entry_price"]) / pos["entry_price"] * 100.0
                    if ret_now < PROFIT_THRESHOLD_PCT:
                        _record_trade(trades, ticker, all_stock_data[ticker]["market"], variant_name, pos,
                                       date_key, c, "時間停損(未達3%)")
                        to_remove.append(ticker); continue
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
                    _record_trade(trades, ticker, all_stock_data[ticker]["market"], variant_name, pos,
                                   date_key, c, "停利(連續2天未創高)")
                    to_remove.append(ticker); continue
                if pos["trailing_low_level"] is not None and l < pos["trailing_low_level"]:
                    _record_trade(trades, ticker, all_stock_data[ticker]["market"], variant_name, pos,
                                   date_key, c, "停利(跌破前K棒)")
                    to_remove.append(ticker); continue

        for t in to_remove:
            del positions[t]

        dualbuy_set = dualbuy_by_day.get(date_key, set())
        candidates = []
        for ticker, data in all_stock_data.items():
            if ticker in positions:
                continue
            info = data["series"].get(date_key)
            if info is None or not info["base_ok"]:
                continue
            bias_val = info["bias"]
            if bias_val is None:
                continue
            if bias_val < FIXED_BIAS_THRESHOLD:
                continue  # 固定用5%門檻,三組都一樣,只有TOP_N會變
            code = ticker.replace(".TWO", "").replace(".TW", "")
            if code not in dualbuy_set:
                continue
            candidates.append((ticker, bias_val, info["close"], info["low"]))

        candidates.sort(key=lambda x: x[1], reverse=True)
        candidates = candidates[:top_n]

        for ticker, bias_val, c, l in candidates:
            positions[ticker] = {
                "entry_date": date_key, "entry_day_idx": day_idx, "entry_price": c,
                "signal_low": l, "highest_close": c, "days_since_new_high": 0,
                "trailing_low_level": None, "activated": False,
            }

    return trades


def _record_trade(trades, ticker, market, variant, pos, exit_date_key, exit_price, reason):
    entry_price = pos["entry_price"]
    ret_pct = (exit_price - entry_price) / entry_price * 100.0
    if abs(ret_pct) > SANITY_MAX_RETURN_PCT:
        return
    entry_d = date.fromisoformat(pos["entry_date"])
    exit_d = date.fromisoformat(exit_date_key)
    holding_days = (exit_d - entry_d).days
    trades.append({
        "ticker": ticker, "market": market, "variant": variant,
        "entry_date": pos["entry_date"], "exit_date": exit_date_key,
        "return_pct": round(ret_pct, 2), "holding_days": holding_days, "exit_reason": reason,
    })


def run_backtest(start_date: date, end_date: date, max_stocks: int | None = None):
    twse_regime = fetch_twii_regime(start_date, end_date)
    otc_regime = fetch_otc_regime_official(start_date, end_date)

    if len(twse_regime) < 30 or len(otc_regime) < 30:
        print("❌ 指數資料抓取不完整,提早中止。")
        return []

    flow_history = fetch_dualbuy_history(start_date, end_date)
    if len(flow_history) < 10:
        print("❌ 外資融資歷史資料太少,提早中止。")
        return []
    dualbuy_by_day = build_dualbuy_qualified_by_day(flow_history)

    print("抓取股票清單...")
    universe = get_universe(include_otc=True)
    if max_stocks:
        universe = universe[:max_stocks]
    print(f"共 {len(universe)} 檔,開始準備每檔股票的訊號序列...")

    days_needed = (date.today() - start_date).days + 30
    yf_period = "2y" if days_needed <= 730 else "5y"

    all_tickers = [row["ticker"] for row in universe]
    ticker_market = {row["ticker"]: row["market"] for row in universe}
    batches = [all_tickers[i:i + BATCH_SIZE] for i in range(0, len(all_tickers), BATCH_SIZE)]

    all_stock_data = {}
    done = 0
    for batch_idx, batch in enumerate(batches, 1):
        print(f"批次 {batch_idx}/{len(batches)}(已處理 {done}/{len(all_tickers)} 檔)")
        batch_data = _fetch_batch(batch, yf_period)
        for t in batch:
            done += 1
            df = batch_data.get(t)
            if df is None:
                continue
            try:
                series = prepare_stock_series(df, ticker_market.get(t, "?"), twse_regime, otc_regime,
                                               start_date, end_date)
                if series:
                    all_stock_data[t] = {"market": ticker_market.get(t, "?"), "series": series}
            except Exception as e:
                print(f"[warn] {t} 準備資料失敗: {e}")
        time.sleep(BATCH_SLEEP)

    print(f"\n共 {len(all_stock_data)} 檔股票準備完成,開始逐日模擬(3種TOP_N)...")

    all_dates = set()
    for data in all_stock_data.values():
        all_dates.update(data["series"].keys())
    master_dates = sorted(all_dates)
    print(f"共同交易日曆:{len(master_dates)} 天")

    all_trades = []
    for variant_name, n in TOPN_VARIANTS.items():
        print(f"\n模擬 {variant_name}(每日取前{n}名)...")
        trades = run_coordinated_simulation(all_stock_data, master_dates, dualbuy_by_day,
                                             n, variant_name)
        print(f"產生 {len(trades)} 筆交易")
        all_trades.extend(trades)

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


def print_report(trades: list[dict], start_date: date, end_date: date):
    print("\n" + "=" * 70)
    print(f"每日候選數量(TOP_N)三方比較({start_date}~{end_date})")
    print("=" * 70)
    for variant_name in TOPN_VARIANTS:
        v_trades = [t for t in trades if t["variant"] == variant_name]
        print(f"\n--- {variant_name} ---")
        _stats_for(v_trades, f"{variant_name} / 全部")
        twse = [t for t in v_trades if t["market"] == "TWSE"]
        tpex = [t for t in v_trades if t["market"] == "TPEX"]
        _stats_for(twse, f"{variant_name} / 上市")
        _stats_for(tpex, f"{variant_name} / 上櫃")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=str, required=True)
    parser.add_argument("--end", type=str, required=True)
    parser.add_argument("--max-stocks", type=int, default=None)
    args = parser.parse_args()

    start_d = datetime.strptime(args.start, "%Y-%m-%d").date()
    end_d = datetime.strptime(args.end, "%Y-%m-%d").date()

    trades = run_backtest(start_d, end_d, max_stocks=args.max_stocks)
    print_report(trades, start_d, end_d)
