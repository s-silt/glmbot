# -*- coding: utf-8 -*-
"""market.py —— 币安 fapi 行情（只读公开接口），429/418 退避一次。"""
from __future__ import annotations

import json
import time
import urllib.request

FAPI = "https://fapi.binance.com"

import re as _re

_SYM_RE = _re.compile(r"^[A-Z0-9]{2,24}USDT$")


def _check_symbol(symbol: str) -> str:
    """防模型幻觉：非空、纯词字符(含中文，币安已有中文名合约如 龙虾USDT)、以 USDT 结尾。"""
    s = str(symbol or "").strip().upper()
    if not _re.match(r"^[\w]{2,28}USDT$", s, _re.UNICODE):
        raise ValueError(f"非法 symbol: {symbol!r}")
    return s


def _q(symbol: str) -> str:
    """URL percent-encode（中文 symbol 必须）。"""
    from urllib.parse import quote
    return quote(_check_symbol(symbol))



def _get(path: str, timeout: int = 20) -> dict | list:
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(FAPI + path, headers={"User-Agent": "3bot-engine/1.0"})
            return json.load(urllib.request.urlopen(req, timeout=timeout))
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "code", None)
            if attempt == 2 or code not in (418, 429, None):
                raise RuntimeError(f"market._get({path}) 失败: {e}") from e
            time.sleep(60)


def top_movers(limit: int = 5, min_qv: float = 5e7) -> list[dict]:
    """24h 涨跌幅榜 ∩ 成交额门槛，返回 [{symbol, change_pct, quote_volume, last_price, high, low}]。"""
    tickers = _get("/fapi/v1/ticker/24hr")
    rows = []
    for t in tickers:
        if not t["symbol"].endswith("USDT"):
            continue
        qv = float(t.get("quoteVolume", 0))
        if qv < min_qv:
            continue
        rows.append({"symbol": t["symbol"], "change_pct": float(t["priceChangePercent"]),
                     "quote_volume": qv, "last_price": float(t["lastPrice"]),
                     "high": float(t["highPrice"]), "low": float(t["lowPrice"])})
    rows.sort(key=lambda r: abs(r["change_pct"]), reverse=True)
    return rows[:limit]


def klines(symbol: str, interval: str = "5m", n: int = 96) -> list[list]:
    symbol = _check_symbol(symbol)
    """K 线（收盘价序列封装见 close_series）。"""
    return _get(f"/fapi/v1/klines?symbol={_q(symbol)}&interval={interval}&limit={n}")


def close_series(symbol: str, interval: str = "5m", n: int = 96) -> list[float]:
    return [float(k[4]) for k in klines(symbol, interval, n)]


def mark_price(symbol: str) -> float:
    symbol = _check_symbol(symbol)
    d = _get(f"/fapi/v1/premiumIndex?symbol={_q(symbol)}")
    return float(d["markPrice"])


def kline_digest(symbol: str) -> str:
    """给模型看的紧凑行情摘要：5m 96 根 + 15m 48 根，只给 OHLCV 聚合特征，不倾倒原始数据。"""
    def digest(interval: str, n: int) -> str:
        ks = klines(symbol, interval, n)
        if not ks:
            return "无数据"
        closes = [float(k[4]) for k in ks]
        vols = [float(k[5]) for k in ks]
        thirds = len(closes) // 3
        seg = lambda arr: "/".join(f"{x:.6g}" for x in arr)
        return (f"前段收盘{seg(closes[:thirds])[:120]}… 中段收盘{seg(closes[thirds:2*thirds])[:120]}… "
                f"近段收盘{seg(closes[2*thirds:])[:160]} 量比(近1/3均量÷全期均量)={sum(vols[2*thirds:])/(thirds or 1)/ (sum(vols)/(len(vols) or 1)):.2f}")
    return f"[{symbol} 5m×96] {digest('5m', 96)}\n[{symbol} 15m×48] {digest('15m', 48)}"
