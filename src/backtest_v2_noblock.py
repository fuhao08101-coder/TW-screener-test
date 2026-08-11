"""
在 v2(整理期停損+跌破前1根K停利)的基礎上,把「近3個月有鉅額交易紀錄」的交易直接排除,
看真正的期望值會不會如預期地提升。

做法:先照 v2 完全一樣的方式跑出全部交易,標記好每筆交易的鉅額交易分組,
再把 block_trade_group == "yes" 的交易從統計中剔除(等於模擬「一開始就不進場」)。
"unknown"(年份太早無法判斷)的交易保留,不強制排除,因為沒有證據說它們有鉅額交易。

其餘規則(篩選、進場、停損、出場)完全跟 v2 一樣,沒有改動。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from backtest_v2 import run_backtest_v2, _stats_for


def print_comparison(all_trades: list[dict]):
    excluded_count = sum(1 for t in all_trades if t.get("block_trade_group") == "yes")
    filtered_trades = [t for t in all_trades if t.get("block_trade_group") != "yes"]

    print("\n" + "=" * 60)
    print("V2 + 排除近3個月有鉅額交易紀錄 —— 對照結果")
    print("=" * 60)
    print(f"\n原始v2交易筆數: {len(all_trades)}")
    print(f"其中確認有鉅額交易而被排除: {excluded_count} 筆")
    print(f"排除後剩餘: {len(filtered_trades)} 筆")

    _stats_for(all_trades, "排除前(原始v2,含鉅額交易股票)")
    _stats_for(filtered_trades, "排除後(v2 + 排除鉅額交易)")

    print("\n" + "=" * 60)
    print("【與前面版本對照】")
    print("  v2(整理期停損+跌破前1根K停利,未排除): 1974筆, 勝率41.7%, 期望值+2.23%")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-stocks", type=int, default=None)
    args = parser.parse_args()

    trades = run_backtest_v2(max_stocks=args.max_stocks)
    print_comparison(trades)
