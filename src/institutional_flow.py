"""
外資+融資雙買濾網(上市TWSE + 上櫃TPEX 都支援)。

資料源:
  上市(TWSE):
    三大法人買賣超日報 T86:https://www.twse.com.tw/rwd/zh/fund/T86
    融資融券餘額 MI_MARGN:https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN
    日期格式:YYYYMMDD(西元年)

  上櫃(TPEX):
    三大法人買賣超:https://www.tpex.org.tw/web/stock/3insti/DAILY_TradE/3itrade_hedge_result.php
    融資融券餘額:https://www.tpex.org.tw/web/stock/margin_trading/margin_balance/margin_bal_result.php
    日期格式:民國年/月/日(例如 115/08/13)
    這兩個網址已經在政府資料開放平台(data.gov.tw)正式登記查證過(dataset 11856、11387),
    信心程度比單純猜測高,但實際JSON欄位結構還沒有機會實測驗證過,這次先用最合理的猜測寫,
    並加上詳細診斷log,如果解析失敗,log會印出實際結構,方便下一輪校正,不會讓整個掃描程式當掉。

判斷規則(上市、上櫃分開抓資料,但套用同一套邏輯,結果合併):
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

TWSE_T86_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
TWSE_MARGIN_URL = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"

TPEX_T86_URL = "https://www.tpex.org.tw/web/stock/3insti/DAILY_TradE/3itrade_hedge_result.php"
TPEX_MARGIN_URL = "https://www.tpex.org.tw/web/stock/margin_trading/margin_balance/margin_bal_result.php"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}
LOOKBACK_TRADING_DAYS = 9
MAX_CALENDAR_DAYS_TO_SCAN = 20
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


# ============ 上市 TWSE ============

def _fetch_t86_twse(date_str: str) -> dict[str, int] | None:
    """date_str格式:YYYYMMDD。回傳 {股票代號: 外資買賣超股數},查無資料回傳 None"""
    try:
        r = requests.get(TWSE_T86_URL, headers=HEADERS,
                          params={"date": date_str, "selectType": "ALL", "response": "json"},
                          timeout=30)
        if r.status_code != 200:
            print(f"[warn] TWSE T86({date_str}) HTTP狀態碼異常: {r.status_code}")
            return None
        payload = r.json()
        if payload.get("stat") != "OK":
            print(f"[info] TWSE T86({date_str}) 非交易日或無資料,stat={payload.get('stat')}")
            return None
        fields = payload.get("fields")
        rows = payload.get("data")
        if not fields or not rows:
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
        print(f"[info] TWSE T86({date_str}) 成功取得 {len(out)} 檔股票外資資料")
        return out
    except Exception as e:
        print(f"[warn] TWSE T86({date_str}) 查詢失敗: {e}")
        return None


def _fetch_margin_twse(date_str: str) -> dict[str, int] | None:
    """date_str格式:YYYYMMDD。回傳 {股票代號: 融資當日增減股數}"""
    try:
        r = requests.get(TWSE_MARGIN_URL, headers=HEADERS,
                          params={"date": date_str, "selectType": "ALL", "response": "json"},
                          timeout=30)
        if r.status_code != 200:
            print(f"[warn] TWSE MI_MARGN({date_str}) HTTP狀態碼異常: {r.status_code}")
            return None
        payload = r.json()
        if payload.get("stat") != "OK":
            print(f"[info] TWSE MI_MARGN({date_str}) 非交易日或無資料,stat={payload.get('stat')}")
            return None

        tables = payload.get("tables") or []
        target_table = None
        for tbl in tables:
            tbl_fields = tbl.get("fields") or []
            if tbl_fields and tbl_fields[0] == "代號" and len(tbl_fields) >= 13:
                target_table = tbl
                break
        if target_table is None:
            print(f"[warn] TWSE MI_MARGN({date_str}) 找不到「代號」開頭的融資融券彙總子表")
            return None

        rows = target_table.get("data") or []
        out = {}
        for row in rows:
            if len(row) < 7:
                continue
            code = (row[0] or "").strip()
            if not code:
                continue
            prev_bal = _to_int(row[5])
            today_bal = _to_int(row[6])
            out[code] = today_bal - prev_bal
        print(f"[info] TWSE MI_MARGN({date_str}) 成功取得 {len(out)} 檔股票融資資料")
        return out
    except Exception as e:
        print(f"[warn] TWSE MI_MARGN({date_str}) 查詢失敗: {e}")
        return None


# ============ 上櫃 TPEX ============

def _roc_date_slash(d: date) -> str:
    """西元date物件轉民國年/月/日字串,例如 2026/08/13 -> 115/08/13"""
    return f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"


def _fetch_t86_tpex(d: date) -> dict[str, int] | None:
    """
    回傳 {股票代號: 外資買賣超股數}。
    實測發現這個端點回傳的是CSV格式(不是JSON),欄位依序為:
    資料日期,代號,名稱,外資及陸資不含外資自營商買進股數,...賣出股數,...買賣超股數,
    外資自營商買進股數,...賣出股數,...買賣超股數,外資及陸資買進股數,外資及陸資賣出股數,
    外資及陸資買賣超股數(這欄已經是前兩者加總,直接用這欄即可),投信...,自營商...
    """
    import csv
    import io

    date_str = _roc_date_slash(d)
    try:
        r = requests.get(TPEX_T86_URL, headers=HEADERS,
                          params={"l": "zh-tw", "se": "EW", "t": "D", "o": "data", "d": date_str},
                          timeout=30)
        if r.status_code != 200:
            print(f"[warn] TPEX 三大法人({date_str}) HTTP狀態碼異常: {r.status_code}")
            return None

        text = r.text.strip()
        if not text:
            print(f"[info] TPEX 三大法人({date_str}) 回應是空的(可能非交易日)")
            return None

        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            return None

        header = rows[0]
        data_rows = rows[1:]

        try:
            idx_code = header.index("代號")
            idx_foreign_total = header.index("外資及陸資買賣超股數")
        except ValueError:
            print(f"[warn] TPEX 三大法人({date_str}) 找不到預期欄位,實際header: {header}")
            return None

        out = {}
        for row in data_rows:
            if len(row) <= max(idx_code, idx_foreign_total):
                continue
            code = row[idx_code].strip()
            if not code:
                continue
            out[code] = _to_int(row[idx_foreign_total])

        print(f"[info] TPEX 三大法人({date_str}) 成功取得 {len(out)} 檔股票外資資料")
        return out
    except Exception as e:
        print(f"[warn] TPEX 三大法人({date_str}) 查詢失敗: {e}")
        return None


def _fetch_margin_tpex(d: date) -> dict[str, int] | None:
    """
    回傳 {股票代號: 融資當日增減股數}。
    這個端點的CSV欄位名稱還沒實際驗證過,先試幾組常見的候選名稱,
    找不到的話會把真實表頭印出來,方便下一輪校正。
    """
    import csv
    import io

    date_str = _roc_date_slash(d)
    try:
        r = requests.get(TPEX_MARGIN_URL, headers=HEADERS,
                          params={"l": "zh-tw", "d": date_str, "o": "data"},
                          timeout=30)
        if r.status_code != 200:
            print(f"[warn] TPEX 融資融券({date_str}) HTTP狀態碼異常: {r.status_code}")
            return None

        text = r.text.strip()
        if not text:
            print(f"[info] TPEX 融資融券({date_str}) 回應是空的(可能非交易日)")
            return None

        # 有些回應可能還是JSON(格式不一定跟三大法人那個端點一致),兩種都試
        if text.startswith("{") or text.startswith("["):
            try:
                payload = r.json()
                print(f"[debug] TPEX 融資融券({date_str}) 回應是JSON,頂層: "
                      f"{list(payload.keys()) if isinstance(payload, dict) else type(payload)}")
            except Exception:
                pass
            # 目前沒有已知的JSON解析規則,先跳過,回報無法解析
            print(f"[warn] TPEX 融資融券({date_str}) 回應是JSON格式,但尚未支援解析,前300字: {text[:300]!r}")
            return None

        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            return None

        header = rows[0]
        data_rows = rows[1:]

        idx_code = None
        for cand in ("代號", "股票代號", "證券代號"):
            if cand in header:
                idx_code = header.index(cand)
                break

        idx_today = None
        idx_prev = None
        today_candidates = ["資今餘額", "融資今餘額", "資今餘額(張)", "今資餘額", "融資今日餘額"]
        prev_candidates = ["資前餘額", "融資前餘額", "資前餘額(張)", "前資餘額", "融資前日餘額"]
        for cand in today_candidates:
            if cand in header:
                idx_today = header.index(cand)
                break
        for cand in prev_candidates:
            if cand in header:
                idx_prev = header.index(cand)
                break

        if idx_code is None or idx_today is None or idx_prev is None:
            print(f"[warn] TPEX 融資融券({date_str}) 找不到預期欄位,實際header: {header}")
            return None

        print(f"[debug] TPEX 融資融券({date_str}) 使用欄位: 代號=header[{idx_code}], "
              f"今餘額=header[{idx_today}]({header[idx_today]}), 前餘額=header[{idx_prev}]({header[idx_prev]})")

        out = {}
        for row in data_rows:
            if len(row) <= max(idx_code, idx_today, idx_prev):
                continue
            code = row[idx_code].strip()
            if not code:
                continue
            today_bal = _to_int(row[idx_today])
            prev_bal = _to_int(row[idx_prev])
            out[code] = today_bal - prev_bal

        print(f"[info] TPEX 融資融券({date_str}) 成功取得 {len(out)} 檔股票融資資料")
        return out
    except Exception as e:
        print(f"[warn] TPEX 融資融券({date_str}) 查詢失敗: {e}")
        return None


# ============ 合併上市櫃,共用邏輯 ============

def _fetch_recent_trading_days_data(n_days: int = LOOKBACK_TRADING_DAYS):
    """
    往回逐日嘗試,湊出 n_days 個有效交易日的 (外資, 融資) 資料,上市櫃合併在一起。
    回傳 [(date_key, foreign_map, margin_map), ...],依日期由舊到新排列。
    只要上市「或」上櫃任一邊當天有抓到資料,就算這天有效;兩邊的股票代號不會重複
    (上市櫃代號不會撞號),所以直接合併字典即可。
    """
    collected = []
    d = date.today()
    tried = 0
    while len(collected) < n_days and tried < MAX_CALENDAR_DAYS_TO_SCAN:
        date_str_twse = d.strftime("%Y%m%d")

        foreign_twse = _fetch_t86_twse(date_str_twse)
        time.sleep(REQUEST_SLEEP)
        margin_twse = _fetch_margin_twse(date_str_twse) if foreign_twse is not None else None
        time.sleep(REQUEST_SLEEP)

        foreign_tpex = _fetch_t86_tpex(d)
        time.sleep(REQUEST_SLEEP)
        margin_tpex = _fetch_margin_tpex(d) if foreign_tpex is not None else None
        time.sleep(REQUEST_SLEEP)

        foreign_map = {}
        margin_map = {}
        if foreign_twse is not None and margin_twse is not None:
            foreign_map.update(foreign_twse)
            margin_map.update(margin_twse)
        if foreign_tpex is not None and margin_tpex is not None:
            foreign_map.update(foreign_tpex)
            margin_map.update(margin_tpex)

        if foreign_map and margin_map:
            collected.append((date_str_twse, foreign_map, margin_map))

        d -= timedelta(days=1)
        tried += 1

    collected.reverse()
    return collected


def build_dual_buy_qualified_set(n_days: int = LOOKBACK_TRADING_DAYS) -> set[str]:
    """回傳目前合格(近n天內外資融資同天雙買過,且之後沒有雙減過)的股票代號集合,上市櫃都含"""
    print(f"抓取近{n_days}個交易日的外資+融資資料(上市+上櫃)...")
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
                break

        if trigger_idx is None:
            continue

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

    print(f"外資融資雙買濾網:合格 {len(qualified)} 檔(上市+上櫃)")
    return qualified
