#!/usr/bin/env python3
"""每日外匯分析：產生下單時機建議

讀取 data/rates.json（由 collect.py 產生），對自選匯率計算：
  1. 交易訊號：趨勢（SMA20/SMA50）＋ MACD 轉折＋ RSI 過濾，
     給出「買進 / 賣出 / 等回檔掛單 / 觀望」與進場、停損、目標價。
  2. 歷史回測：同一套規則在過去資料上的交易次數、勝率、平均報酬。
  3. 進口付款換匯時機：外幣兌新台幣在近一年區間的位置（便宜 / 普通 / 偏貴）。

輸出：
  data/signals.json —— 看盤網頁讀取
  data/report.md    —— 每日分析報告（GitHub 上可直接閱讀）

只使用 Python 標準函式庫。訊號僅依每日收盤中價計算，僅供參考，不構成投資建議。
"""
import datetime as dt
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

NAMES = {
    "TWD": "新台幣", "USD": "美元", "EUR": "歐元", "JPY": "日圓", "GBP": "英鎊",
    "CNY": "人民幣", "HKD": "港幣", "AUD": "澳幣", "CAD": "加幣", "CHF": "瑞士法郎",
    "SGD": "新加坡幣", "KRW": "韓元", "XAU": "黃金",
}
PAIRS = [("USD", "TWD"), ("EUR", "TWD"), ("JPY", "TWD"), ("CNY", "TWD"), ("GBP", "TWD"),
         ("HKD", "TWD"), ("AUD", "TWD"), ("EUR", "USD"), ("USD", "JPY"), ("GBP", "USD"),
         ("USD", "CNY"), ("XAU", "USD")]
IMPORT_CURS = ["USD", "EUR", "JPY", "CNY", "GBP", "AUD", "HKD", "SGD", "KRW", "CHF"]

STOP_ATR, TARGET_ATR, MAX_HOLD = 2.0, 3.0, 20  # 停損 2 倍、目標 3 倍日均波幅；最多持有 20 個交易日


# ---------- 指標 ----------
def sma(a, n):
    out, s = [math.nan] * len(a), 0.0
    for i, x in enumerate(a):
        s += x
        if i >= n:
            s -= a[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(a, n):
    out, k, e = [math.nan] * len(a), 2 / (n + 1), None
    for i, x in enumerate(a):
        if math.isnan(x):
            continue
        e = x if e is None else x * k + e * (1 - k)
        out[i] = e
    return out


def rsi(a, n=14):
    out, g, l = [math.nan] * len(a), 0.0, 0.0
    for i in range(1, len(a)):
        ch = a[i] - a[i - 1]
        up, dn = max(ch, 0), max(-ch, 0)
        if i <= n:
            g += up / n
            l += dn / n
        else:
            g = (g * (n - 1) + up) / n
            l = (l * (n - 1) + dn) / n
        if i >= n:
            out[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def macd_hist(a):
    f, s = ema(a, 12), ema(a, 26)
    line = [f[i] - s[i] if i >= 25 else math.nan for i in range(len(a))]
    sig = ema(line, 9)
    return [line[i] - sig[i] for i in range(len(a))]


def atr_close(a, n=14):
    """只有收盤價時，以日變動絕對值的平均近似 ATR。"""
    d = [math.nan] + [abs(a[i] - a[i - 1]) for i in range(1, len(a))]
    out = [math.nan] * len(a)
    for i in range(n, len(a)):
        out[i] = sum(d[i - n + 1:i + 1]) / n
    return out


def ok(*xs):
    return all(not math.isnan(x) for x in xs)


# ---------- 訊號 ----------
def indicators(v):
    return {"s20": sma(v, 20), "s50": sma(v, 50), "rsi": rsi(v), "h": macd_hist(v), "atr": atr_close(v)}


def signal_at(v, ind, i):
    """回傳 (動作, 原因清單)。動作：long / short / wait_long / wait_short / none"""
    s20, s50, r, h = ind["s20"][i], ind["s50"][i], ind["rsi"][i], ind["h"]
    if i < 3 or not ok(s20, s50, r, h[i], h[i - 1], h[i - 2]):
        return "none", ["資料不足"]
    p = v[i]
    up, dn = s20 > s50 and p > s50, s20 < s50 and p < s50
    cross_up = h[i] > 0 and min(h[i - 1], h[i - 2]) <= 0
    cross_dn = h[i] < 0 and max(h[i - 1], h[i - 2]) >= 0
    if up and cross_up and r < 70:
        return "long", ["上升趨勢（SMA20 在 SMA50 之上）", "MACD 柱轉正（動能回升）", f"RSI {r:.0f} 未過熱"]
    if dn and cross_dn and r > 30:
        return "short", ["下降趨勢（SMA20 在 SMA50 之下）", "MACD 柱轉負（動能轉弱）", f"RSI {r:.0f} 未超賣"]
    if up:
        why = ["上升趨勢中，尚未出現新的進場轉折"]
        if r >= 70:
            why.append(f"RSI {r:.0f} 過熱，不宜追價")
        return "wait_long", why
    if dn:
        why = ["下降趨勢中，尚未出現新的進場轉折"]
        if r <= 30:
            why.append(f"RSI {r:.0f} 超賣，不宜追空")
        return "wait_short", why
    return "none", ["均線糾結、方向不明"]


def backtest(v, ind):
    trades, i, n = [], 0, len(v)
    while i < n - 1:
        act, _ = signal_at(v, ind, i)
        if act not in ("long", "short") or not ok(ind["atr"][i]):
            i += 1
            continue
        side = 1 if act == "long" else -1
        entry, a = v[i], ind["atr"][i]
        stop, target = entry - side * STOP_ATR * a, entry + side * TARGET_ATR * a
        j = i + 1
        while j < n:
            p = v[j]
            if side * (p - stop) <= 0 or side * (p - target) >= 0 or j - i >= MAX_HOLD:
                break
            opp, _ = signal_at(v, ind, j)
            if opp == ("short" if side == 1 else "long"):
                break
            j += 1
        j = min(j, n - 1)
        trades.append(side * (v[j] / entry - 1))
        i = j + 1
    wins = [t for t in trades if t > 0]
    total = 1.0
    for t in trades:
        total *= 1 + t
    return {
        "trades": len(trades),
        "winRate": round(len(wins) / len(trades), 3) if trades else None,
        "avgReturn": round(sum(trades) / len(trades), 5) if trades else None,
        "totalReturn": round(total - 1, 5) if trades else None,
    }


# ---------- 主流程 ----------
def load():
    with open(os.path.join(DATA, "rates.json"), encoding="utf-8") as f:
        rates = json.load(f)["rates"]
    # 只保留週一到週五（週末外匯休市，資料為持平）
    days = [d for d in sorted(rates) if dt.date.fromisoformat(d).weekday() < 5]
    return days, rates


def pair_series(days, rates, b, q):
    ds, v = [], []
    for d in days:
        r = rates[d]
        rb = 1.0 if b == "USD" else r.get(b)
        rq = 1.0 if q == "USD" else r.get(q)
        if rb and rq:
            ds.append(d)
            v.append(rq / rb)
    return ds, v


def rnd(x, ref):
    if x is None or math.isnan(x):
        return None
    a = abs(ref)
    return round(x, 2 if a >= 1000 else 3 if a >= 10 else 4 if a >= 1 else 6)


LABEL = {"long": "買進訊號", "short": "賣出訊號", "wait_long": "偏多・等回檔掛買",
         "wait_short": "偏空・等反彈掛賣", "none": "觀望"}


def analyze_pair(days, rates, b, q):
    ds, v = pair_series(days, rates, b, q)
    if len(v) < 60:
        return None
    ind = indicators(v)
    i = len(v) - 1
    act, why = signal_at(v, ind, i)
    p, a, s20 = v[i], ind["atr"][i], ind["s20"][i]
    entry = stop = target = None
    if act == "long":
        entry, stop, target = p, p - STOP_ATR * a, p + TARGET_ATR * a
    elif act == "short":
        entry, stop, target = p, p + STOP_ATR * a, p - TARGET_ATR * a
    elif act == "wait_long":
        entry = min(p, max(s20, p - a))  # 回檔到 SMA20 附近掛買，最多拉回 1 倍日均波幅
        stop, target = entry - STOP_ATR * a, entry + TARGET_ATR * a
        why.append(f"建議回檔至 {rnd(entry, p)}（SMA20 附近）再掛單買進" if entry < p else "價格已回到 SMA20 之下，可於現價附近分批買進")
    elif act == "wait_short":
        entry = max(p, min(s20, p + a))
        stop, target = entry + STOP_ATR * a, entry - TARGET_ATR * a
        why.append(f"建議反彈至 {rnd(entry, p)}（SMA20 附近）再掛單賣出" if entry > p else "價格已反彈到 SMA20 之上，可於現價附近分批賣出")
    bt = backtest(v, ind)
    return {
        "pair": f"{b}/{q}", "base": b, "quote": q,
        "name": f"{NAMES.get(b, b)}/{NAMES.get(q, q)}",
        "date": ds[i], "price": rnd(p, p),
        "action": act, "label": LABEL[act], "reasons": why,
        "entry": rnd(entry, p), "stop": rnd(stop, p), "target": rnd(target, p),
        "rsi": round(ind["rsi"][i], 1), "atrPct": round(a / p, 5),
        "backtest": bt, "reliable": bool(bt["trades"] >= 5 and bt["avgReturn"] and bt["avgReturn"] > 0),
    }


def analyze_import(days, rates, cur):
    ds, v = pair_series(days, rates, cur, "TWD")
    v = v[-250:]  # 約一年交易日
    if len(v) < 60:
        return None
    p, lo, hi = v[-1], min(v), max(v)
    pct = (p - lo) / (hi - lo) if hi > lo else 0.5
    r = rsi(v)[-1]
    if pct <= 0.25:
        label, tip = "相對便宜", "匯價在近一年低檔，有外幣付款需求可提前分批買進"
    elif pct >= 0.75:
        label, tip = "偏貴", "匯價在近一年高檔，非急需付款可等待拉回再換"
    else:
        label, tip = "普通", "位於區間中段，依付款時程分批換匯、降低匯率風險"
    return {"cur": cur, "name": NAMES.get(cur, cur), "price": rnd(p, p), "low": rnd(lo, p),
            "high": rnd(hi, p), "avg": rnd(sum(v) / len(v), p), "pct": round(pct, 3),
            "rsi": round(r, 1), "label": label, "tip": tip}


SESSIONS = [  # 台灣時間（夏令／冬令）
    {"name": "亞洲盤（東京、台北、香港、新加坡）", "summer": "08:00–16:00", "winter": "08:00–16:00",
     "note": "新台幣、日圓、人民幣主要交易時段；台北外匯市場 09:00–16:00，銀行牌告在此時段更新最頻繁"},
    {"name": "歐洲盤（倫敦）", "summer": "15:00–23:30", "winter": "16:00–00:30",
     "note": "歐元、英鎊、瑞士法郎成交量最大"},
    {"name": "美洲盤（紐約）", "summer": "20:00–05:00", "winter": "21:00–06:00",
     "note": "美國經濟數據常於 20:30（夏令）／21:30（冬令）公布，波動加劇"},
    {"name": "歐美重疊時段（流動性最佳）", "summer": "20:00–23:30", "winter": "21:00–00:30",
     "note": "點差最小、成交最活絡，主要貨幣對最適合下單的時段"},
]


def fmt_pct(x):
    return "—" if x is None else f"{x * 100:+.2f}%"


def write_report(out):
    L = [f"# 外匯每日分析報告 {out['date']}", "",
         "> 依每日收盤國際中價計算，僅供參考，不構成投資建議。下單前請搭配即時報價與重大經濟事件確認。", "",
         "## 今日下單時機", "",
         "| 匯率 | 現價 | 建議 | 進場參考 | 停損 | 目標 | RSI | 回測勝率（次數） |",
         "|---|---:|---|---:|---:|---:|---:|---:|"]
    footnote = "⚠ 表示此規則在該匯率過去平均報酬為負，參考性低。"

    for s in out["signals"]:
        bt = s["backtest"]
        wr = "—" if bt["winRate"] is None else f"{bt['winRate'] * 100:.0f}%（{bt['trades']}）" + ("" if s["reliable"] else " ⚠")
        L.append(f"| {s['pair']} {s['name']} | {s['price']} | **{s['label']}** | {s['entry'] or '—'} | "
                 f"{s['stop'] or '—'} | {s['target'] or '—'} | {s['rsi']} | {wr} |")
    L += ["", footnote, "", "### 訊號說明", ""]
    for s in out["signals"]:
        if s["action"] != "none":
            L.append(f"- **{s['pair']} {s['label']}**：" + "；".join(s["reasons"]))
    L += ["", "## 進口付款換匯時機（外幣兌新台幣，近一年）", "",
          "| 外幣 | 現價 | 一年低 | 一年高 | 區間位置 | 判斷 | 建議 |", "|---|---:|---:|---:|---:|---|---|"]
    for m in out["import"]:
        L.append(f"| {m['cur']} {m['name']} | {m['price']} | {m['low']} | {m['high']} | "
                 f"{m['pct'] * 100:.0f}% | **{m['label']}** | {m['tip']} |")
    L += ["", "## 一天中適合下單的時段（台灣時間）", "", "| 時段 | 夏令（3–11 月） | 冬令（11–3 月） | 說明 |", "|---|---|---|---|"]
    for s in SESSIONS:
        L.append(f"| {s['name']} | {s['summer']} | {s['winter']} | {s['note']} |")
    L += ["", "## 規則說明", "",
          "- **買進訊號**：SMA20 在 SMA50 之上且價格站上 SMA50，MACD 柱於近 3 日由負轉正，RSI < 70。",
          "- **賣出訊號**：條件相反（SMA20 在 SMA50 之下，MACD 柱轉負，RSI > 30）。",
          "- **等回檔／反彈掛單**：趨勢成立但尚無轉折，建議在 SMA20 附近掛限價單。",
          f"- **停損／目標**：{STOP_ATR:g} 倍／{TARGET_ATR:g} 倍日均波幅（報酬風險比 1:{TARGET_ATR / STOP_ATR:.1f}），最多持有 {MAX_HOLD} 個交易日。",
          f"- **回測**：同一規則在 {out['from']} 至 {out['date']} 的收盤資料上模擬，不含點差與手續費；過去表現不代表未來。", ""]
    with open(os.path.join(DATA, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def main():
    days, rates = load()
    if len(days) < 60:
        print("資料不足，請先執行 collect.py", file=sys.stderr)
        return 1
    out = {
        "date": days[-1], "from": days[0],
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "rules": {"stopAtr": STOP_ATR, "targetAtr": TARGET_ATR, "maxHold": MAX_HOLD},
        "signals": [s for s in (analyze_pair(days, rates, b, q) for b, q in PAIRS) if s],
        "import": [m for m in (analyze_import(days, rates, c) for c in IMPORT_CURS) if m],
        "sessions": SESSIONS,
    }
    with open(os.path.join(DATA, "signals.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    write_report(out)
    counts = {}
    for s in out["signals"]:
        counts[s["label"]] = counts.get(s["label"], 0) + 1
    print(f"{out['date']} 分析完成：" + "、".join(f"{k} {v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
