# -*- coding: utf-8 -*-
"""GLMBOT_MOCK=1：拦截 llm.chat / market 公开接口，干跑全流程（不花钱、不联网）。"""
from __future__ import annotations

import json
import os

import ledger
import llm
import market

MOCK_MOVERS = [
    {"symbol": "BTCUSDT", "change_pct": 5.2, "quote_volume": 1.2e8,
     "last_price": 50000.0, "high": 51200.0, "low": 47800.0},
    {"symbol": "ETHUSDT", "change_pct": -3.8, "quote_volume": 8.0e7,
     "last_price": 3000.0, "high": 3150.0, "low": 2880.0},
    {"symbol": "SOLUSDT", "change_pct": 4.1, "quote_volume": 6.5e7,
     "last_price": 150.0, "high": 158.0, "low": 142.0},
]

_SCOUT = {
    "rev": 0,
    "verdict": "CANDIDATE",
    "symbol": "BTCUSDT",
    "side": "LONG",
    "entry": 50000,
    "sl": 49500,
    "tp": 51000,
    "lev": 5,
    "margin": 30,
    "理由": "mock 结构突破，跌破 SL 即证伪",
    "风险点": "mock 波动扩大",
    "learned": "mock scout candidate",
}
_ADV = {"pass": True, "理由": "mock 未发现致命伤", "learned": "mock adv pass"}
_TRADER = {
    "would_block": False,
    "verdict": "OK_OPEN",
    "reasons": ["mock dry-run ok"],
    "learned": "mock trader ok_open",
}
_GRADE = {
    "research": 7,
    "risk": 8,
    "discipline": 9,
    "hard_fail": "无",
    "soft_fail": "无",
    "评语": "mock 中评，结构完整但理由偏模板",
}
_REFLECT = {"learned": "mock reflect learned", "profile_patch": None}

_applied = False


def _role_from_call(alias: str, system: str) -> str:
    try:
        import roles
        getter = getattr(roles, "current_role", None)
        cur = getter() if callable(getter) else getattr(roles, "_current_role", None)
        if cur in ("Scout", "Adversary", "Trader"):
            return cur
    except Exception:
        pass
    head = (system or "").lstrip()
    if head.startswith("# Trader"):
        return "Trader"
    if head.startswith("# Adversary"):
        return "Adversary"
    if head.startswith("# Scout"):
        return "Scout"
    if alias == "flash":
        return "Trader"
    return "Scout"


def _mock_chat(alias: str, system: str, user: str, max_tokens: int = 4096, timeout: int = 180, **_k) -> str:
    sys, usr = system or "", user or ""
    if "你是阅卷官" in sys:
        return json.dumps(_GRADE, ensure_ascii=False)
    if "自我反思" in usr or "profile_patch" in usr:
        return json.dumps(_REFLECT, ensure_ascii=False)
    role = _role_from_call(alias, system)
    if role == "Trader":
        return json.dumps(_TRADER, ensure_ascii=False)
    if role == "Adversary":
        return json.dumps(_ADV, ensure_ascii=False)
    return json.dumps(_SCOUT, ensure_ascii=False)


def _mock_chat_astra(system: str, user: str) -> tuple[str, str]:
    return json.dumps({"approve": True, "理由": "mock"}, ensure_ascii=False), "codex"


def _mock_top_movers(limit: int = 5, min_qv: float = 5e7) -> list[dict]:
    return list(MOCK_MOVERS[: max(1, int(limit))])


def _mock_mark_price(symbol: str) -> float:
    factor = 1.02 if os.environ.get("GLMBOT_MOCK_MOVE") == "1" else 1.001
    st = ledger.load_state()
    pos = st.get("open_position") or {}
    entry = pos.get("entry")
    if entry not in (None, ""):
        return float(entry) * factor
    for row in MOCK_MOVERS:
        if row["symbol"] == symbol:
            return float(row["last_price"]) * factor
    return 50000.0 * factor


def _mock_kline_digest(symbol: str) -> str:
    return f"[{symbol} 5m×96] mock digest\n[{symbol} 15m×48] mock digest"


def _mock_klines(symbol: str, interval: str = "5m", n: int = 96) -> list[list]:
    px = 50000.0
    for row in MOCK_MOVERS:
        if row["symbol"] == symbol:
            px = float(row["last_price"])
            break
    return [[0, px, px, px, px, 1.0] for _ in range(int(n))]


def _mock_close_series(symbol: str, interval: str = "5m", n: int = 96) -> list[float]:
    return [float(k[4]) for k in _mock_klines(symbol, interval, n)]


def _blocked(*_a, **_k):
    raise RuntimeError("GLMBOT_MOCK=1：禁止联网")


def apply() -> None:
    """幂等。仅当 GLMBOT_MOCK=1 时打补丁。"""
    global _applied
    if os.environ.get("GLMBOT_MOCK") != "1":
        return
    if _applied:
        return
    llm.chat = _mock_chat
    llm.chat_astra = _mock_chat_astra
    market.top_movers = _mock_top_movers
    market.mark_price = _mock_mark_price
    market.kline_digest = _mock_kline_digest
    market.klines = _mock_klines
    market.close_series = _mock_close_series
    market._get = _blocked
    _applied = True


if os.environ.get("GLMBOT_MOCK") == "1":
    apply()
