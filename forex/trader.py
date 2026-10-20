#!/usr/bin/env python3
"""外匯自動交易（模擬帳戶）

依 analyze.py 的同一套訊號規則，每天自動「下單、移動停損、停利／停損出場」，
並記錄損益。不連接任何券商、不動用真實資金。

兩個帳戶：
  live    —— 從 trader_config.json 的 start 日期起，每日自動模擬交易
  history —— 用過去全部資料做同一套流程的歷史模擬（只用當時已知的資料判斷，無偷看未來）

規則：
  進場：出現「買進訊號／賣出訊號」，且（onlyReliable 時）該匯率截至當日的回測平均報酬為正
  部位：每筆最多虧損帳戶的 riskPct（預設 1%），依停損距離換算部位大小
  出場：收盤觸及目標價（停利）／停損價、反向訊號、持有超過 20 個交易日；
        獲利達 breakevenAtr 倍日均波幅後，停損移到進場價（保本）
  成本：依匯率類型扣除估計點差

輸出 data/trades.json，並在 data/report.md 加上模擬交易摘要。
只使用 Python 標準函式庫。僅供研究參考，不構成投資建議。
"""
import datetime as dt
import json
import math
import os
import sys

import analyze as A

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "trader_config.json")
OUT = os.path.join(A.DATA, "trades.json")

DEFAULTS = {
    "start": "2026-10-20",     # 模擬帳戶開始日
    "capital": 1000000,        # 起始資金（新台幣）
    "riskPct": 0.01,           # 每筆最大虧損占帳戶比例
    "maxOpen": 5,              # 同時最多持有部位數
    "onlyReliable": True,      # 只交易回測平均報酬為正的匯率
    "breakevenAtr": 1.0,       # 獲利達幾倍日均波幅後移到保本停損
    "minHistoryTrades": 5,     # 判斷可靠性至少需要的歷史交易次數
}
# 估計單邊交易成本（占價格比例）
COST = {"TWD": 0.0010, "XAU": 0.0005}
DEFAULT_COST = 0.0002


def cost_of(b, q):
    return max(COST.get(b, DEFAULT_COST), COST.get(q, DEFAULT_COST))


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG):
        with open(CONFIG, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def twd_per(rates_day, cur):
    """1 單位 cur 等於多少新台幣。"""
    usd_twd = rates_day.get("TWD")
    per_usd = 1.0 if cur == "USD" else rates_day.get(cur)
    return usd_twd / per_usd if usd_twd and per_usd else None


class Market:
    """每個匯率的價格序列與指標，依日期查詢。"""

    def __init__(self, days, rates):
        self.days, self.rates, self.pairs = days, rates, {}
        for b, q in A.PAIRS:
            ds, v = A.pair_series(days, rates, b, q)
            if len(v) >= 60:
                self.pairs[(b, q)] = {"ds": ds, "v": v, "idx": {d: i for i, d in enumerate(ds)},
                                      "ind": A.indicators(v), "rel": {}}

    def reliable(self, key, i, cfg):
        """只用 i 之前（含）已知的資料回測，避免偷看未來。"""
        p = self.pairs[key]
        if i not in p["rel"]:
            v = p["v"][:i + 1]
            ind = {k: a[:i + 1] for k, a in p["ind"].items()}
            bt = A.backtest(v, ind)
            p["rel"][i] = bool(bt["trades"] >= cfg["minHistoryTrades"] and bt["avgReturn"] and bt["avgReturn"] > 0)
        return p["rel"][i]


def simulate(mkt, cfg, start):
    equity, open_pos, closed, curve = float(cfg["capital"]), [], [], []
    days = [d for d in mkt.days if d >= start]
    for d in days:
        # 1) 先處理持倉：保本停損、停利、停損、反向訊號、持有期滿
        still = []
        for pos in open_pos:
            p = mkt.pairs[(pos["base"], pos["quote"])]
            i = p["idx"].get(d)
            if i is None:
                still.append(pos)
                continue
            price, side = p["v"][i], pos["side"]
            pos["held"] += 1
            pos["last"] = price
            if not pos["breakeven"] and side * (price - pos["entry"]) >= cfg["breakevenAtr"] * pos["atr"]:
                pos["stop"], pos["breakeven"] = pos["entry"], True
            act, _ = A.signal_at(p["v"], p["ind"], i)
            reason = None
            if side * (price - pos["target"]) >= 0:
                reason = "停利"
            elif side * (price - pos["stop"]) <= 0:
                reason = "保本出場" if pos["breakeven"] else "停損"
            elif act == ("short" if side == 1 else "long"):
                reason = "反向訊號"
            elif pos["held"] >= A.MAX_HOLD:
                reason = "持有期滿"
            if reason:
                r_mult = side * (price - pos["entry"]) / pos["risk"] - pos["costR"]
                pnl = pos["riskAmt"] * r_mult
                equity += pnl
                closed.append({**{k: pos[k] for k in ("pair", "dir", "openDate", "entry", "units")},
                               "closeDate": d, "exit": A.rnd(price, price), "reason": reason,
                               "r": round(r_mult, 2), "pnl": round(pnl), "pct": round(pnl / (equity - pnl), 5)})
            else:
                still.append(pos)
        open_pos = still

        # 2) 新進場（收盤價成交）
        held_pairs = {pos["pair"] for pos in open_pos}
        for (b, q), p in mkt.pairs.items():
            if len(open_pos) >= cfg["maxOpen"]:
                break
            pair = f"{b}/{q}"
            i = p["idx"].get(d)
            if i is None or pair in held_pairs:
                continue
            act, why = A.signal_at(p["v"], p["ind"], i)
            if act not in ("long", "short"):
                continue
            if cfg["onlyReliable"] and not mkt.reliable((b, q), i, cfg):
                continue
            price, atr = p["v"][i], p["ind"]["atr"][i]
            if not A.ok(atr) or atr <= 0:
                continue
            side = 1 if act == "long" else -1
            risk = A.STOP_ATR * atr
            risk_amt = equity * cfg["riskPct"]
            q_twd = twd_per(mkt.rates[d], q)
            units = risk_amt / q_twd / risk if q_twd else None
            open_pos.append({
                "pair": pair, "base": b, "quote": q, "side": side, "dir": "買進" if side == 1 else "賣出",
                "openDate": d, "entry": A.rnd(price, price), "atr": atr, "risk": risk,
                "stop": price - side * risk, "target": price + side * A.TARGET_ATR * atr,
                "riskAmt": risk_amt, "costR": 2 * cost_of(b, q) * price / risk,
                "units": round(units) if units else None, "held": 0, "breakeven": False,
                "last": price, "why": why,
            })
            held_pairs.add(pair)

        # 3) 每日權益（含未實現損益）
        unreal = sum(pos["riskAmt"] * (pos["side"] * (pos["last"] - pos["entry"]) / pos["risk"]) for pos in open_pos)
        curve.append([d, round(equity + unreal)])

    wins = [t for t in closed if t["pnl"] > 0]
    peak, mdd = -math.inf, 0.0
    for _, e in curve:
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1)
    return {
        "start": start, "capital": cfg["capital"], "equity": round(equity),
        "equityWithOpen": curve[-1][1] if curve else round(equity),
        "returnPct": round((curve[-1][1] if curve else equity) / cfg["capital"] - 1, 5),
        "trades": len(closed), "winRate": round(len(wins) / len(closed), 3) if closed else None,
        "maxDrawdown": round(mdd, 5),
        "open": [{
            "pair": p["pair"], "dir": p["dir"], "openDate": p["openDate"], "entry": p["entry"],
            "stop": A.rnd(p["stop"], p["entry"]), "target": A.rnd(p["target"], p["entry"]),
            "last": A.rnd(p["last"], p["entry"]), "units": p["units"], "breakeven": p["breakeven"],
            "unrealized": round(p["riskAmt"] * p["side"] * (p["last"] - p["entry"]) / p["risk"]),
            "why": p["why"],
        } for p in open_pos],
        "closed": closed[::-1],
        "curve": curve,
    }


def fmt_money(x):
    return f"{x:,.0f}"


def append_report(out):
    path = os.path.join(A.DATA, "report.md")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        text = f.read().split("\n## 自動交易（模擬帳戶）")[0].rstrip() + "\n"
    L = ["", "## 自動交易（模擬帳戶）", "",
         "> 不連接券商、不動用真實資金；依收盤價模擬成交，已扣估計點差。"]
    for key, title in (("live", "模擬帳戶"), ("history", "歷史模擬")):
        a = out[key]
        wr = "—" if a["winRate"] is None else f"{a['winRate'] * 100:.0f}%"
        L += ["", f"### {title}（{a['start']} 起，起始資金 {fmt_money(a['capital'])} 元）", "",
              f"- 權益：**{fmt_money(a['equityWithOpen'])} 元**（報酬 {a['returnPct'] * 100:+.2f}%，最大回檔 {a['maxDrawdown'] * 100:.2f}%）",
              f"- 已平倉 {a['trades']} 筆，勝率 {wr}；持倉 {len(a['open'])} 筆"]
        if key == "live" and a["open"]:
            L += ["", "| 匯率 | 方向 | 進場日 | 進場價 | 停損 | 目標 | 現價 | 未實現損益 |", "|---|---|---|---:|---:|---:|---:|---:|"]
            for p in a["open"]:
                L.append(f"| {p['pair']} | {p['dir']} | {p['openDate']} | {p['entry']} | {p['stop']}{'（保本）' if p['breakeven'] else ''} | "
                         f"{p['target']} | {p['last']} | {fmt_money(p['unrealized'])} |")
        if a["closed"]:
            L += ["", "| 匯率 | 方向 | 進場 | 出場 | 出場原因 | R | 損益 |", "|---|---|---|---|---|---:|---:|"]
            for t in a["closed"][:10]:
                L.append(f"| {t['pair']} | {t['dir']} | {t['openDate']} @ {t['entry']} | {t['closeDate']} @ {t['exit']} | "
                         f"{t['reason']} | {t['r']:+.2f} | {fmt_money(t['pnl'])} |")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text + "\n".join(L) + "\n")


def main():
    cfg = load_config()
    days, rates = A.load()
    if len(days) < 60:
        print("資料不足，請先執行 collect.py", file=sys.stderr)
        return 1
    mkt = Market(days, rates)
    out = {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "date": days[-1], "config": cfg,
        "live": simulate(mkt, cfg, cfg["start"]),
        "history": simulate(mkt, cfg, days[0]),
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    append_report(out)
    for key in ("live", "history"):
        a = out[key]
        print(f"[{key}] {a['start']} 起：權益 {fmt_money(a['equityWithOpen'])}（{a['returnPct'] * 100:+.2f}%），"
              f"平倉 {a['trades']} 筆，持倉 {len(a['open'])} 筆")
    return 0


if __name__ == "__main__":
    sys.exit(main())
