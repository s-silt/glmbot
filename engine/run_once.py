# -*- coding: utf-8 -*-
"""run_once.py —— 一轮：持仓复核 → graph.invoke → 记忆回收 → round+1。"""
from __future__ import annotations

import traceback

import mock as glmbot_mock
glmbot_mock.apply()

import graph
import ledger
import market
from roles import ask_role, extract_json

TIME_EXIT_HOURS = 3.0


def _facts(pos: dict, mark: float) -> dict:
    entry = float(pos["entry"])
    sl, tp = float(pos["sl"]), float(pos["tp"])
    side = str(pos.get("side") or "")
    opened = pos.get("opened_at")
    age_h = 0.0
    t = None
    try:
        from datetime import datetime, timezone
        t = datetime.strptime(str(opened), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - t).total_seconds() / 3600.0
    except (TypeError, ValueError):
        age_h = 0.0
    if side == "SHORT":
        hit_sl = mark >= sl
        hit_tp = mark <= tp
    else:
        hit_sl = mark <= sl
        hit_tp = mark >= tp
    return {
        "symbol": pos.get("symbol"),
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "lev": pos.get("lev"),
        "margin": pos.get("margin"),
        "opened_at": opened,
        "mark": mark,
        "dist_sl_pct": (mark - sl) / entry if entry else 0.0,
        "dist_tp_pct": (tp - mark) / entry if entry else 0.0,
        "age_hours": round(age_h, 4),
        "hit_sl": bool(hit_sl),
        "hit_tp": bool(hit_tp),
        "time_exit": age_h >= TIME_EXIT_HOURS,
    }


def _code_verdict(facts: dict) -> str:
    if facts.get("hit_sl"):
        return "CLOSE_SL"
    if facts.get("hit_tp"):
        return "CLOSE_TP"
    if facts.get("time_exit"):
        return "CLOSE_TIME"
    return "HOLD"


def _review_position(st: dict) -> tuple[dict, list[str], str]:
    """有持仓则复核。返回 (新state, errors, 复核verdict)。"""
    errors: list[str] = []
    pos = st.get("open_position")
    if not pos:
        return st, errors, ""
    mark = float(market.mark_price(str(pos["symbol"])))
    facts = _facts(pos, mark)
    code_v = _code_verdict(facts)
    prompt = (
        "持仓复核。下面是代码算好的数值事实，请只输出判定 JSON："
        "verdict ∈ HOLD / CLOSE_SL / CLOSE_TP / CLOSE_TIME / CLOSE_INVALID。\n"
        f"{facts}"
    )
    raw = ask_role("Trader", prompt)
    try:
        tv = extract_json(raw)
        trader_v = str(tv.get("verdict") or "")
    except ValueError as e:
        errors.append(f"持仓复核 JSON 失败: {e}")
        trader_v = ""
    final = code_v
    if trader_v and trader_v != code_v:
        errors.append(f"持仓复核方向不一致: trader={trader_v} code={code_v}，以代码为准")
        final = code_v
    elif trader_v:
        final = trader_v
        if final != code_v:
            final = code_v
            errors.append(f"持仓复核方向不一致: trader={trader_v} code={code_v}，以代码为准")

    if final.startswith("CLOSE"):
        entry = float(pos["entry"])
        lev = int(float(pos["lev"]))
        margin = float(pos.get("margin") or 30)
        side = str(pos.get("side") or "LONG")
        pnl = ledger.pnl(side, entry, mark, lev, margin)
        ledger.append_journal([
            ledger.now_utc(), str(st.get("round", "")), "SIM_CLOSE",
            str(pos.get("symbol", "")), side, str(entry), str(pos.get("sl", "")),
            str(pos.get("tp", "")), str(lev), str(margin), str(mark),
            f"{pnl:.6g}", "", "Trader",
        ])
        st["bankroll"] = float(st.get("bankroll") or 0) + pnl
        st["open_position"] = None
        ledger.save_state(st)
    return st, errors, final


def _recycle(result: dict) -> None:
    learned = dict(result.get("learned") or {})
    action = str(result.get("action") or "")
    topic_type = str(result.get("topic_type") or "")
    for role in ("Scout", "Adversary", "Trader"):
        text = learned.get(role) or f"{action} {topic_type}".strip()
        ledger.append_log(role, text)


def run() -> None:
    try:
        glmbot_mock.apply()
        st = ledger.load_state()
        st, review_errors, review_v = _review_position(st)
        partial: dict = {}
        if review_errors:
            partial["errors"] = list(review_errors)
        result = graph.invoke(partial or None)
        _recycle(result)
        st = ledger.load_state()
        done_round = int(st.get("round") or 0)
        st["round"] = done_round + 1
        ledger.save_state(st)
        grade = result.get("grade") or {}
        score = f"{grade.get('research', '')}/{grade.get('risk', '')}/{grade.get('discipline', '')}"
        pos = st.get("open_position") or {}
        pos_s = f"{pos.get('symbol', '')} {pos.get('side', '')}".strip() or "flat"
        lines = [
            f"round={done_round} type={result.get('topic_type')} action={result.get('action')}",
            f"score={score} hard_fail={grade.get('hard_fail') or []}",
            f"bankroll={st.get('bankroll')} open={pos_s}",
        ]
        if review_v:
            lines.append(f"持仓复核={review_v}")
        if result.get("errors") or review_errors:
            lines.append(f"errors={list(result.get('errors') or review_errors)[:2]}")
        print("\n".join(lines[:5]))
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}")
        errp = ledger.RUNTIME / "errors.log"
        with errp.open("a", encoding="utf-8") as f:
            f.write(f"{ledger.now_utc()} {type(e).__name__}: {e}\n")
            f.write(traceback.format_exc() + "\n")


if __name__ == "__main__":
    run()
