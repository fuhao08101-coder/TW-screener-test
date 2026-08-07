"""
單一股票診斷工具:輸入一個股票代號,把每一項篩選條件的實際數值印出來,
方便找出「這檔股票為什麼沒被抓到」。

用法(在 GitHub Actions 裡透過 debug.yml 執行,輸入代號即可,不用自己在電腦跑):
    python src/debug_ticker.py 6414
"""
import sys
import pandas as pd
import yfinance as yf

from screener import (
    LOOKBACK_DAYS, BIAS_MA_PERIOD, BIAS_THRESHOLD, LONG_MA_PERIOD, BIAS_DIRECTION,
    MA87_BREACH_LOOKBACK, SECOND_MA_PERIOD, REQUIRE_MA_ALIGNMENT,
    ATR_PERIOD, ATR_MIN_THRESHOLD, ATR_MIN_PCT_THRESHOLD, REQUIRE_ATR_MIN,
    HISTORY_PERIOD, _calc_atr,
)


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
                print(f"  → 但夠算SMA87,會改走「新股簡化規則」(只看乖離+SMA87,不看SMA284/ATR14)")
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
    diagnose(sys.argv[1])
