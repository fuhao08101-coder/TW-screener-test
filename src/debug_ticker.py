"""
單一股票診斷工具:輸入一個股票代號,把每一項篩選條件的實際數值印出來,
方便找出「這檔股票為什麼沒被抓到」。

用法(在 GitHub Actions 裡透過 debug.yml 執行,輸入代號即可,不用自己在電腦跑):
    python src/debug_ticker.py 6414
"""
import sys
import pandas as pd
import yfinance as yf

from universe import get_universe
from screener import (
    ATR_PERIOD, ATR_MIN_THRESHOLD, ATR_MIN_PCT_THRESHOLD, REQUIRE_ATR_MIN,
    LOOKBACK_DAYS, BIAS_MA_PERIOD, BIAS_THRESHOLD, LONG_MA_PERIOD, BIAS_DIRECTION,
    MA87_BREACH_LOOKBACK, SECOND_MA_PERIOD, REQUIRE_MA_ALIGNMENT, HISTORY_PERIOD,
    _calc_atr, _fetch_batch,
)


def diagnose_new_listing(df: pd.DataFrame):
    """對應 screener.py 的 _evaluate_new_listing,逐條印出判斷結果"""
    close = df["Close"].dropna()
    ma15 = close.rolling(BIAS_MA_PERIOD).mean()
    ma87 = close.rolling(LONG_MA_PERIOD).mean()
    bias = (close - ma15) / ma15 * 100.0

    lookback = min(LOOKBACK_DAYS, len(close))
    recent_bias = bias.tail(lookback)

    max_bias = recent_bias.max()
    min_bias = recent_bias.min()

    print(f"  【條件1】近{lookback}日乖離(方向={BIAS_DIRECTION}, 門檻={BIAS_THRESHOLD}%)")
    print(f"     近{lookback}日最大乖離: {max_bias:.2f}%  最小乖離: {min_bias:.2f}%")
    cond1 = (max_bias >= BIAS_THRESHOLD) if BIAS_DIRECTION == "up" else \
            (min_bias <= -BIAS_THRESHOLD) if BIAS_DIRECTION == "down" else \
            (max_bias >= BIAS_THRESHOLD or min_bias <= -BIAS_THRESHOLD)
    print(f"     {'✅ 通過' if cond1 else '❌ 沒通過'}")

    latest_close = close.iloc[-1]
    latest_ma87 = ma87.iloc[-1]
    print(f"\n  【條件2】收盤({latest_close:.2f}) > SMA87({latest_ma87:.2f})")
    cond2 = latest_close > latest_ma87
    print(f"     {'✅ 通過' if cond2 else '❌ 沒通過'}")

    breach_lookback = min(MA87_BREACH_LOOKBACK, len(close))
    recent_close_87 = close.tail(breach_lookback)
    recent_ma87_87 = ma87.tail(breach_lookback)
    breach_days = (recent_close_87 < recent_ma87_87).sum()
    print(f"\n  【條件3】近{breach_lookback}日內跌破87MA的天數: {breach_days} 天")
    cond3 = breach_days == 0
    print(f"     {'✅ 通過(0天跌破)' if cond3 else f'❌ 沒通過(有{breach_days}天跌破)'}")

    if breach_days > 0:
        breach_dates = recent_close_87[recent_close_87 < recent_ma87_87].index
        print(f"     跌破的日期: {[d.strftime('%Y-%m-%d') for d in breach_dates]}")

    overall = cond1 and cond2 and cond3
    print(f"\n  ===> 綜合結果(新股規則): {'✅ 應該要被抓到' if overall else '❌ 被剔除,上面第一個❌就是原因'}")


def diagnose(code: str):
    for suffix, market in [(".TW", "TWSE"), (".TWO", "TPEX")]:
        ticker = f"{code}{suffix}"
        print(f"\n===== 嘗試 {ticker}({market}) =====")
        try:
            df = yf.Ticker(ticker).history(period=HISTORY_PERIOD, auto_adjust=True)
        except Exception as e:
            print(f"  抓取失敗: {e}")
            continue

        if df is None or df.empty:
            print("  查無資料(可能代號錯誤,或這個市場沒有這檔股票)")
            continue

        print(f"  取得 {len(df)} 筆歷史資料,期間 {df.index[0].date()} ~ {df.index[-1].date()}")

        min_len = max(LONG_MA_PERIOD, SECOND_MA_PERIOD, ATR_PERIOD) + 30
        if len(df) < min_len:
            print(f"  資料筆數不足完整規則(需要至少 {min_len} 筆,只有 {len(df)} 筆)")
            if len(df) >= LONG_MA_PERIOD + 10:
                print(f"  → 夠算SMA87,走「新股簡化規則」,繼續檢查各條件:\n")
                diagnose_new_listing(df)
            else:
                print(f"  → 連SMA87都算不出來(需要至少{LONG_MA_PERIOD+10}筆),完全被排除")
            continue

        close = df["Close"].dropna()
        ma15 = close.rolling(BIAS_MA_PERIOD).mean()
        ma87 = close.rolling(LONG_MA_PERIOD).mean()
        ma_second = close.rolling(SECOND_MA_PERIOD).mean()
        bias = (close - ma15) / ma15 * 100.0
        atr = _calc_atr(df, ATR_PERIOD)

        latest_close = close.iloc[-1]
        latest_ma87 = ma87.iloc[-1]
        latest_ma_second = ma_second.iloc[-1]
        latest_atr = atr.iloc[-1]
        latest_atr_pct = (latest_atr / latest_close * 100.0) if not pd.isna(latest_atr) else None

        recent_bias = bias.tail(LOOKBACK_DAYS)
        max_bias = recent_bias.max()
        min_bias = recent_bias.min()

        recent_close_87 = close.tail(MA87_BREACH_LOOKBACK)
        recent_ma87_87 = ma87.tail(MA87_BREACH_LOOKBACK)
        breach_days = (recent_close_87 < recent_ma87_87).sum()

        print(f"\n  【條件1】近{LOOKBACK_DAYS}日乖離(方向={BIAS_DIRECTION}, 門檻={BIAS_THRESHOLD}%)")
        print(f"     近{LOOKBACK_DAYS}日最大乖離: {max_bias:.2f}%  最小乖離: {min_bias:.2f}%")
        cond1 = (max_bias >= BIAS_THRESHOLD) if BIAS_DIRECTION == "up" else \
                (min_bias <= -BIAS_THRESHOLD) if BIAS_DIRECTION == "down" else \
                (max_bias >= BIAS_THRESHOLD or min_bias <= -BIAS_THRESHOLD)
        print(f"     {'✅ 通過' if cond1 else '❌ 沒通過'}")

        print(f"\n  【條件2】收盤({latest_close:.2f}) > SMA87({latest_ma87:.2f})")
        cond2 = latest_close > latest_ma87
        print(f"     {'✅ 通過' if cond2 else '❌ 沒通過'}")

        print(f"\n  【條件3】近{MA87_BREACH_LOOKBACK}日內跌破87MA的天數: {breach_days} 天")
        cond3 = breach_days == 0
        print(f"     {'✅ 通過(0天跌破)' if cond3 else f'❌ 沒通過(有{breach_days}天跌破)'}")

        print(f"\n  【條件4】SMA87({latest_ma87:.2f}) > SMA{SECOND_MA_PERIOD}({latest_ma_second:.2f})")
        cond4 = (latest_ma87 > latest_ma_second) if REQUIRE_MA_ALIGNMENT else True
        print(f"     {'✅ 通過' if cond4 else '❌ 沒通過'}")

        print(f"\n  【條件5】ATR{ATR_PERIOD} 絕對值 {latest_atr:.2f}(門檻>={ATR_MIN_THRESHOLD}) 且 佔股價 {latest_atr_pct:.2f}%(門檻>={ATR_MIN_PCT_THRESHOLD}%)")
        cond5 = (
            not pd.isna(latest_atr) and latest_atr >= ATR_MIN_THRESHOLD and
            latest_atr_pct is not None and latest_atr_pct >= ATR_MIN_PCT_THRESHOLD
        ) if REQUIRE_ATR_MIN else True
        print(f"     {'✅ 通過' if cond5 else '❌ 沒通過'}")

        overall = cond1 and cond2 and cond3 and cond4 and cond5
        print(f"\n  ===> 綜合結果: {'✅ 應該要被抓到' if overall else '❌ 被剔除,上面第一個❌就是原因'}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python src/debug_ticker.py 股票代號(例如 6414)")
        sys.exit(1)

    code = sys.argv[1]
    diagnose(code)

    print(f"\n\n===== 檢查掃描流程是否會漏掉這檔 =====")
    print("步驟A:檢查證交所/櫃買清單裡有沒有這個代號...")
    universe = get_universe(include_otc=True)
    matched = [row for row in universe if row["code"] == code]
    if not matched:
        print(f"  ❌ 清單裡完全沒有代號 {code},問題出在 universe.py 抓取的清單漏了它")
        print(f"     (可能原因:交易所公開資料剛好沒收錄、或代號格式不符)")
    else:
        for row in matched:
            ticker = row["ticker"]
            print(f"  ✅ 清單裡有:{row['name']}({ticker}), 市場={row['market']}")

            print(f"\n步驟B:模擬正式掃描的「批次下載」方式,單獨測試這一檔會不會抓到資料...")
            batch_result = _fetch_batch([ticker])
            if ticker in batch_result and not batch_result[ticker].empty:
                print(f"  ✅ 批次下載方式(_fetch_batch)成功抓到 {len(batch_result[ticker])} 筆資料")
                print(f"     → 代表批次下載本身沒問題,如果實際掃描還是漏掉,")
                print(f"       可能是「跟其他上百檔一起大批次抓取時」才會不穩定,")
                print(f"       單獨測試看不出來,建議之後把 BATCH_SIZE 調小一點試試")
            else:
                print(f"  ❌ 批次下載方式(_fetch_batch)抓不到這檔的資料!")
                print(f"     → 這就是問題所在:yf.download 批次模式對這檔股票不穩定")
                print(f"       單獨用 yf.Ticker() 查詢(debug工具用的方式)沒問題,")
                print(f"       但正式掃描用的批次方式會漏掉它")
