"""
外資+融資雙買濾網(目前僅支援上市TWSE,上櫃TPEX的對應端點欄位格式尚未驗證,暫不支援)。

資料源:
  三大法人買賣超日報 T86:https://www.twse.com.tw/rwd/zh/fund/T86
    外資買賣超 = 外陸資買賣超股數(不含外資自營商) + 外資自營商買賣超股數
  融資融券餘額 MI_MARGN:https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN
    融資當日增減 = 融資今日餘額 - 融資前日餘額(同一列資料就有這兩個欄位,不用跨日相減)

這兩個端點都是「指定日期、一次回傳當天全市場資料」,不是逐股查詢,
所以抓 N 個交易日,只需要 N 次請求(不是 N x 股票數次),速度可以接受。

判斷規則:
  近 LOOKBACK_TRADING_DAYS 個交易日內,找「外資買超 且 融資增加」同一天發生的日子(觸發日)。
  沒有觸發日 → 不合格。
  有觸發日 → 檢查從觸發日到最新一天之間,有沒有出現「外資賣超 且 融資減少」同一天發生
             (雙減),只要出現一次就整檔排除。
  兩個條件都通過 → 合格。
"""
from __future__ import annotations
import time
from datetime import date, timedelta
import requests

T86_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
MARGIN_URL = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}
LOOKBACK_TRADING_DAYS = 9
MAX_CALENDAR_DAYS_TO_SCAN = 20  # 往回找幾個「日曆天」,扣掉假日湊出9個交易日
REQUEST_SLEEP = 0.3


def _to_int(s):
    if s is None:
        return 0
    try:
        s = str(s).replace(",", "").strip()
        if s in ("", "-", "X"):
            return 0
        return int(float(s))
    except (ValueError, TypeError):
        return 0


def _fetch_t86(date_str: str) -> dict[str, int] | None:
    """回傳 {股票代號: 外資買賣超股數},查無資料(非交易日)回傳 None"""
    try:
        r = requests.get(T86_URL, headers=HEADERS,
                          params={"date": date_str, "selectType": "ALL", "response": "json"},
                          timeout=30)
        if r.status_code != 200:
            print(f"[warn] T86({date_str}) HTTP狀態碼異常: {r.status_code}")
            return None
        payload = r.json()
        stat = payload.get("stat")
        if stat != "OK":
            print(f"[info] T86({date_str}) 非交易日或無資料,stat={stat}")
            return None
        fields = payload.get("fields")
        rows = payload.get("data")
        if not fields or not rows:
            print(f"[warn] T86({date_str}) 回傳格式異常,fields或data是空的")
            return None
        out = {}
        for row in rows:
            d = dict(zip(fields, row))
            code = (d.get("證券代號") or "").strip()
            if not code:
                continue
            foreign = (
                _to_int(d.get("外陸資買賣超股數(不含外資自營商)"))
                + _to_int(d.get("外資自營商買賣超股數"))
            )
            out[code] = foreign
        print(f"[info] T86({date_str}) 成功取得 {len(out)} 檔股票外資資料")
        return out
    except Exception as e:
        print(f"[warn] T86({date_str}) 查詢失敗: {e}")
        return None


def _fetch_margin(date_str: str) -> dict[str, int] | None:
    """回傳 {股票代號: 融資當日增減股數(今日餘額-前日餘額)},查無資料回傳 None"""
    try:
        r = requests.get(MARGIN_URL, headers=HEADERS,
                          params={"date": date_str, "selectType": "ALL", "response": "json"},
                          timeout=30)
        if r.status_code != 200:
            print(f"[warn] MI_MARGN({date_str}) HTTP狀態碼異常: {r.status_code}")
            return None
        payload = r.json()
        stat = payload.get("stat")
        if stat != "OK":
            print(f"[info] MI_MARGN({date_str}) 非交易日或無資料,stat={stat}")
            return None

        tables = payload.get("tables") or []
        target_table = None
        for tbl in tables:
            tbl_fields = tbl.get("fields") or []
            # 找「代號」開頭、有夠多欄位的那個子表(融資融券彙總),
            # 不能只看第一個子表,因為子表0是另一種「信用交易統計」彙總表
            if tbl_fields and tbl_fields[0] == "代號" and len(tbl_fields) >= 13:
                target_table = tbl
                break

        if target_table is None:
            print(f"[warn] MI_MARGN({date_str}) 找不到「代號」開頭的融資融券彙總子表")
            return None

        rows = target_table.get("data") or []
        out = {}
        for row in rows:
            if len(row) < 7:
                continue
            code = (row[0] or "").strip()
            if not code:
                continue
            # 用「位置」取值,不能用欄位名稱——因為「買進/賣出/前日餘額/今日餘額」
            # 這幾個名稱在表裡重複出現兩次(前段是融資、後段是融券),用名稱對應會抓錯組
            # 位置:0=代號 1=名稱 2=買進 3=賣出 4=現金償還 5=前日餘額 6=今日餘額(以上都是融資)
            prev_bal = _to_int(row[5])
            today_bal = _to_int(row[6])
            out[code] = today_bal - prev_bal

        print(f"[info] MI_MARGN({date_str}) 成功取得 {len(out)} 檔股票融資資料")
        return out
        out = {}
        for row in rows:
            d = dict(zip(fields, row))
            code = (d.get("股票代號") or "").strip()
            if not code:
                continue
            today_bal = _to_int(d.get("融資今日餘額"))
            prev_bal = _to_int(d.get("融資前日餘額"))
            out[code] = today_bal - prev_bal
        print(f"[info] MI_MARGN({date_str}) 成功取得 {len(out)} 檔股票融資資料")
        return out
    except Exception as e:
        print(f"[warn] MI_MARGN({date_str}) 查詢失敗: {e}")
        return None


def _fetch_recent_trading_days_data(n_days: int = LOOKBACK_TRADING_DAYS):
    """
    往回逐日嘗試,湊出 n_days 個有效交易日的 (外資, 融資) 資料。
    回傳 [(date_str, foreign_map, margin_map), ...],依日期由舊到新排列。
    """
    collected = []
    d = date.today()
    tried = 0
    while len(collected) < n_days and tried < MAX_CALENDAR_DAYS_TO_SCAN:
        date_str = d.strftime("%Y%m%d")
        foreign_map = _fetch_t86(date_str)
        time.sleep(REQUEST_SLEEP)
        if foreign_map is not None:
            margin_map = _fetch_margin(date_str)
            time.sleep(REQUEST_SLEEP)
            if margin_map is not None:
                collected.append((date_str, foreign_map, margin_map))
        d -= timedelta(days=1)
        tried += 1
    collected.reverse()  # 由舊到新
    return collected


def build_dual_buy_qualified_set(n_days: int = LOOKBACK_TRADING_DAYS) -> set[str]:
    """
    回傳目前合格(近n天內外資融資同天雙買過,且之後沒有雙減過)的上市股票代號集合。
    """
    print(f"抓取近{n_days}個交易日的外資+融資資料...")
    daily_data = _fetch_recent_trading_days_data(n_days)

    if len(daily_data) < 2:
        print("[warn] 外資/融資資料抓取失敗或資料太少,這個濾網這次會讓所有股票被排除")
        return set()

    print(f"實際取得 {len(daily_data)} 個交易日的資料:{[d[0] for d in daily_data]}")

    all_codes: set[str] = set()
    for _, foreign_map, margin_map in daily_data:
        all_codes.update(foreign_map.keys())
        all_codes.update(margin_map.keys())

    qualified: set[str] = set()
    for code in all_codes:
        trigger_idx = None
        for idx, (_, foreign_map, margin_map) in enumerate(daily_data):
            foreign_net = foreign_map.get(code, 0)
            margin_chg = margin_map.get(code, 0)
            if foreign_net > 0 and margin_chg > 0:
                trigger_idx = idx
                break  # 找到最早的雙買日就當觸發日

        if trigger_idx is None:
            continue  # 沒有雙買日,不合格

        has_dual_sell_after = False
        for idx in range(trigger_idx, len(daily_data)):
            _, foreign_map, margin_map = daily_data[idx]
            foreign_net = foreign_map.get(code, 0)
            margin_chg = margin_map.get(code, 0)
            if foreign_net < 0 and margin_chg < 0:
                has_dual_sell_after = True
                break

        if not has_dual_sell_after:
            qualified.add(code)

    print(f"外資融資雙買濾網:合格 {len(qualified)} 檔(上市)")
    return qualified
