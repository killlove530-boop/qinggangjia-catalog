#!/usr/bin/env python3
"""外匯資料收集器

從免費公開的匯率 API（fawazahmed0/exchange-api，每日更新，涵蓋新台幣 TWD、
黃金 XAU 等 200+ 種貨幣）下載每日匯率，存成：

  data/rates.json  —— 看盤網頁會優先讀取（加速載入、離線可用）
  data/rates.csv   —— 方便用 Excel 開啟分析

所有匯率皆以「1 美元 = ? 單位貨幣」儲存，任兩種貨幣的交叉匯率可自行換算：
  A/B = rate[B] / rate[A]

用法：
  python3 collect.py              # 增量更新（預設回補近 365 天）
  python3 collect.py --days 730   # 回補近兩年
  python3 collect.py --workers 8  # 同時下載數

只使用 Python 標準函式庫，不需安裝任何套件。
"""
import argparse
import csv
import datetime as dt
import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

CURRENCIES = [
    "twd", "eur", "jpy", "gbp", "cny", "hkd", "aud", "cad", "chf",
    "sgd", "krw", "nzd", "thb", "myr", "php", "idr", "vnd", "inr", "xau",
]
# 資料源最早日期（該套件自 2024-03-02 起提供歷史快照）
EARLIEST = dt.date(2024, 3, 2)
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
JSON_PATH = os.path.join(DATA_DIR, "rates.json")
CSV_PATH = os.path.join(DATA_DIR, "rates.csv")


def urls_for(day):
    tag = "latest" if day is None else day.isoformat()
    return [
        f"https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@{tag}/v1/currencies/usd.min.json",
        f"https://{tag}.currency-api.pages.dev/v1/currencies/usd.min.json",
    ]


def fetch(day):
    """回傳 (實際資料日期, {貨幣: 匯率})；失敗回傳 None。"""
    for url in urls_for(day):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "forex-collector/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                payload = json.load(r)
            usd = payload["usd"]
            rates = {c.upper(): usd[c] for c in CURRENCIES if c in usd}
            return payload.get("date") or day.isoformat(), rates
        except Exception as e:  # noqa: BLE001 - 換下一個鏡像
            last = e
    print(f"  ! {day or 'latest'} 下載失敗：{last}", file=sys.stderr)
    return None


def load_existing():
    if os.path.exists(JSON_PATH):
        with open(JSON_PATH, encoding="utf-8") as f:
            return json.load(f).get("rates", {})
    return {}


def save(rates):
    os.makedirs(DATA_DIR, exist_ok=True)
    dates = sorted(rates)
    codes = [c.upper() for c in CURRENCIES]
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "base": "USD",
            "source": "fawazahmed0/exchange-api",
            "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "rates": {d: rates[d] for d in dates},
        }, f, ensure_ascii=False, separators=(",", ":"))
    with open(CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date"] + [f"USD/{c}" for c in codes])
        for d in dates:
            w.writerow([d] + [rates[d].get(c, "") for c in codes])


def main():
    ap = argparse.ArgumentParser(description="下載每日外匯資料")
    ap.add_argument("--days", type=int, default=365, help="回補天數（預設 365）")
    ap.add_argument("--workers", type=int, default=6, help="同時下載數（預設 6）")
    args = ap.parse_args()

    rates = load_existing()
    today = dt.date.today()
    start = max(EARLIEST, today - dt.timedelta(days=args.days))
    missing = [start + dt.timedelta(days=i) for i in range((today - start).days)
               if (start + dt.timedelta(days=i)).isoformat() not in rates]
    print(f"已有 {len(rates)} 天資料，需下載 {len(missing)} 天 + 最新報價")

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(fetch, missing + [None]))
    ok = 0
    for res in results:
        if res:
            day, r = res
            rates[day] = r
            ok += 1
    if not ok:
        print("全部下載失敗，未寫入檔案（請檢查網路連線）", file=sys.stderr)
        return 1
    save(rates)
    print(f"完成：成功 {ok} 筆，共 {len(rates)} 天 → {os.path.relpath(JSON_PATH)}, {os.path.relpath(CSV_PATH)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
