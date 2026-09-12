# -*- coding: utf-8 -*-
"""graph.py —— LangGraph 状态机：Coach → Scout → Adv∥Trader → gate → final_review → price_check → execute → grade → reflect → apply_patch。"""
from __future__ import annotations

import json
import operator
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Annotated, TypedDict, Any

from langgraph.graph import END, START, StateGraph

import ledger
import llm
import market
from roles import ask_role, extract_json

MARGIN = 30.0
LEV_MIN, LEV_MAX = 5, 10
DELTA_MIN, DELTA_MAX = 0.005, 0.08
COOLDOWN_HOURS = 24.0
PRICE_DEV_MAX = 0.003
FINAL_REVIEW_DAILY_CAP = 8
CURRICULUM = ledger.RUNTIME / "curriculum.json"
SYNTHETIC_DIR = ledger.RUNTIME / "synthetic"

SCOUT_REQUIRED = ("rev", "verdict", "symbol", "side", "entry", "sl", "tp", "lev", "理由", "风险点", "learned")
TRADER_REQUIRED = ("would_block", "verdict", "reasons", "learned")
PATCH_TIMES = ledger.RUNTIME / "memory" / "patch_times.json"
PATCH_DEBOUNCE = timedelta(hours=24)
RISK_KEEP_RE = re.compile(r"不准|禁止|必须|宁可|铁律")
BAD_PATCH_RE = re.compile(r"提高杠杆|放大仓位|降低门槛|跳过审核|无视")
ALLOWED_ROLES = ("Scout", "Adversary", "Trader")


class GraphState(TypedDict):
    round: int
    topic_type: str
    topic: str
    material: str
    scout_card: dict
    adv_verdict: dict | None
    adv_used: bool
    trader_verdict: dict
    gate_ok: bool
    gate_reason: str
    action: str
    grade: dict
    learned: dict[str, str]
    errors: Annotated[list[str], operator.add]
    adv_just_ran: bool
    final_review_timeout: bool
    reflects: dict


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.strptime(str(ts).strip(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _load_curriculum() -> dict:
    if not CURRICULUM.exists():
        return {"type_queue": ["LIVE_READ"], "retest_queue": []}
    return json.loads(CURRICULUM.read_text(encoding="utf-8"))


def _save_curriculum(cur: dict) -> None:
    CURRICULUM.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")


def _public_card(card: dict) -> dict:
    return {k: v for k, v in (card or {}).items() if not str(k).startswith("_")}


def _cooldown_symbols() -> set[str]:
    blocked: set[str] = set()
    if not ledger.LEDGER.exists():
        return blocked
    now = _now()
    for line in ledger.LEDGER.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cols = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cols) < 4:
            continue
        ts, event, symbol = cols[0], cols[2], cols[3]
        if event in ("ts_utc", "---", "event") or not symbol:
            continue
        if event not in ("SIM_CLOSE", "GATE_BLOCK", "NO_TRADE"):
            continue
        t = _parse_ts(ts)
        if t is None:
            continue
        if (now - t).total_seconds() <= COOLDOWN_HOURS * 3600:
            blocked.add(symbol)
    return blocked


def _fnum(v: Any, default: float | None = None) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _journal(event: str, rnd: int, card: dict, *, exit_: str = "", pnl: str = "",
             score: str = "", by: str = "") -> None:
    ledger.append_journal([
        ledger.now_utc(),
        str(rnd),
        event,
        str(card.get("symbol", "") or ""),
        str(card.get("side", "") or ""),
        "" if card.get("entry") in (None, "") else str(card.get("entry")),
        "" if card.get("sl") in (None, "") else str(card.get("sl")),
        "" if card.get("tp") in (None, "") else str(card.get("tp")),
        "" if card.get("lev") in (None, "") else str(card.get("lev")),
        "" if card.get("margin") in (None, "") else str(card.get("margin")),
        exit_,
        pnl,
        score,
        by,
    ])


def _latest_synthetic() -> str:
    files = [p for p in SYNTHETIC_DIR.glob("*.json") if p.is_file()] if SYNTHETIC_DIR.exists() else []
    if not files:
        return ""
    latest = max(files, key=lambda p: p.stat().st_mtime)
    return latest.read_text(encoding="utf-8")


def coach_node(state: GraphState) -> dict:
    errors: list[str] = []
    cur = _load_curriculum()
    q = list(cur.get("type_queue") or [])
    if q:
        topic_type = str(q.pop(0))
        cur["type_queue"] = q
        _save_curriculum(cur)
    else:
        topic_type = "LIVE_READ"

    topic = ""
    material = ""
    if topic_type == "LIVE_READ":
        movers = market.top_movers(5)
        if not movers:
            errors.append("top_movers 空")
            topic = ""
            material = json.dumps({"movers": []}, ensure_ascii=False)
        else:
            # 候选轮换出题：避免永远出涨跌幅榜首（极端行情天然触发"贴极值不追"→课程单一化）
            first = movers[int(state.get("round", 0)) % len(movers)]
            topic = str(first.get("symbol", ""))
            digest = market.kline_digest(topic) if topic else ""
            material = json.dumps({"mover": first, "kline_digest": digest}, ensure_ascii=False)
    else:
        # SYNTHETIC / RETEST：原样传材料，代码不检查缺陷
        raw = _latest_synthetic()
        if not raw:
            errors.append(f"{topic_type} 无 synthetic 材料")
            material = "{}"
            topic = topic_type
        else:
            material = raw
            try:
                obj = json.loads(raw)
                topic = str(obj.get("topic") or obj.get("symbol") or obj.get("题面") or topic_type)
            except json.JSONDecodeError:
                topic = topic_type
    return {"topic_type": topic_type, "topic": topic, "material": material, "errors": errors}


def scout_node(state: GraphState) -> dict:
    errors: list[str] = []
    prev = dict(state.get("scout_card") or {})
    redone = bool(prev.get("_redone"))
    hint = ""
    if redone:
        hint = (
            f"\n# 打回重做\n上一版研究卡（须 rev+1，不得引用旧 PASS）：\n"
            f"{json.dumps(_public_card(prev), ensure_ascii=False)}\n"
            f"Adversary：{json.dumps(state.get('adv_verdict') or {}, ensure_ascii=False)}\n"
        )
    prompt = (
        f"# 题型\n{state.get('topic_type')}\n"
        f"# 题面\n{state.get('topic')}\n"
        f"# 材料\n{state.get('material')}\n"
        f"{hint}"
        "# 输出\n只输出一张研究卡 JSON（字段见岗位 profile）。不要 Markdown 叙述。"
    )
    raw = ask_role("Scout", prompt)
    try:
        card = extract_json(raw)
    except ValueError as e:
        errors.append(f"Scout JSON 失败: {e}")
        card = {
            "rev": int(_fnum(prev.get("rev"), 0) or 0) + (1 if redone else 0),
            "verdict": "DATA_GAP",
            "symbol": state.get("topic") or "",
            "side": "HOLD",
            "entry": 0,
            "sl": 0,
            "tp": 0,
            "lev": 5,
            "理由": "JSON解析失败",
            "风险点": "",
            "learned": "输出必须是合法JSON",
        }
    if redone:
        card["_redone"] = True
        try:
            card["rev"] = max(int(_fnum(card.get("rev"), 0) or 0), int(_fnum(prev.get("rev"), 0) or 0))
        except (TypeError, ValueError):
            card["rev"] = int(_fnum(prev.get("rev"), 1) or 1)
    learned = {}
    if card.get("learned"):
        learned["Scout"] = str(card["learned"])
    return {"scout_card": card, "learned": learned, "errors": errors}


def adversary_gate(state: GraphState) -> str:
    card = state.get("scout_card") or {}
    if card.get("verdict") == "CANDIDATE" and not state.get("adv_used"):
        return "adversary"
    return "trader"


def _adv_pass(adv: dict | None) -> bool:
    if not adv:
        return False
    v = adv.get("pass")
    if v is True:
        return True
    if isinstance(v, str) and v.strip().lower() in ("true", "pass", "yes", "1"):
        return True
    return False


def adversary_node(state: GraphState) -> dict:
    errors: list[str] = []
    card = dict(state.get("scout_card") or {})
    prompt = (
        "只审以下研究卡 JSON，输出 pass JSON。不要直呼 Trader。\n"
        f"{json.dumps(_public_card(card), ensure_ascii=False)}"
    )
    raw = ask_role("Adversary", prompt)
    try:
        adv = extract_json(raw)
    except ValueError as e:
        errors.append(f"Adversary JSON 失败: {e}")
        adv = {"pass": False, "理由": "JSON解析失败", "learned": "输出必须是合法JSON"}

    learned = {}
    if adv.get("learned"):
        learned["Adversary"] = str(adv["learned"])
    elif _adv_pass(adv):
        learned["Adversary"] = str(adv.get("理由") or "pass")

    if _adv_pass(adv):
        return {"adv_verdict": adv, "adv_used": True, "learned": learned, "errors": errors}

    # kill
    if card.get("_redone"):
        return {
            "adv_verdict": adv,
            "adv_used": True,
            "action": "NO_TRADE",
            "gate_ok": False,
            "gate_reason": "adversary kill after redo",
            "learned": learned,
            "errors": errors,
        }
    card["_redone"] = True
    card["rev"] = int(_fnum(card.get("rev"), 0) or 0) + 1
    return {
        "scout_card": card,
        "adv_verdict": adv,
        "adv_used": True,
        "learned": learned,
        "errors": errors,
    }


def after_adversary(state: GraphState) -> str:
    if _adv_pass(state.get("adv_verdict")):
        return "trader"
    if state.get("action") == "NO_TRADE":
        return "execute"
    return "scout"


def trader_node(state: GraphState) -> dict:
    errors: list[str] = []
    st = ledger.load_state()
    snap = {
        "open_position": st.get("open_position"),
        "cooldown": sorted(_cooldown_symbols()),
        "fapi": {"status": "ok", "submit_enabled": False},
        "halt": bool(st.get("halt")),
        "bankroll": st.get("bankroll"),
        "session": st.get("session"),
        "single_position": True,
        "margin": MARGIN,
        "lev_range": [LEV_MIN, LEV_MAX],
    }
    prompt = (
        "对研究卡做 DRY_RUN 判定（审执行资格，不依赖 Adversary 结论），只输出 would_block JSON。\n"
        f"# 研究卡\n{json.dumps(_public_card(state.get('scout_card') or {}), ensure_ascii=False)}\n"
        f"# 执行状态快照\n{json.dumps(snap, ensure_ascii=False)}"
    )
    raw = ask_role("Trader", prompt)
    try:
        tv = extract_json(raw)
    except ValueError as e:
        errors.append(f"Trader JSON 失败: {e}")
        tv = {
            "would_block": True,
            "verdict": "DATA_GAP",
            "reasons": ["JSON解析失败"],
            "learned": "输出必须是合法JSON",
        }
    learned = {}
    if tv.get("learned"):
        learned["Trader"] = str(tv["learned"])
    return {"trader_verdict": tv, "learned": learned, "errors": errors}


def adv_trader_node(state: GraphState) -> dict:
    """CANDIDATE 且尚未送审：Adv∥Trader 并行；否则只跑 Trader。闸门仍等 Adv 结论。"""
    if adversary_gate(state) != "adversary":
        out = trader_node(state)
        out["adv_just_ran"] = False
        return out
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_adv = pool.submit(adversary_node, state)
        fut_trd = pool.submit(trader_node, state)
        adv_out = fut_adv.result()
        trd_out = fut_trd.result()
    learned: dict[str, str] = {}
    learned.update(trd_out.get("learned") or {})
    learned.update(adv_out.get("learned") or {})
    errors = list(trd_out.get("errors") or []) + list(adv_out.get("errors") or [])
    return {**trd_out, **adv_out, "learned": learned, "errors": errors, "adv_just_ran": True}


def after_adv_trader(state: GraphState) -> str:
    if not state.get("adv_just_ran"):
        return "gate"
    if _adv_pass(state.get("adv_verdict")):
        return "gate"
    if state.get("action") == "NO_TRADE":
        return "execute"
    return "scout"


def _gate_reasons(card: dict, st: dict) -> list[str]:
    reasons: list[str] = []
    entry, sl, tp = _fnum(card.get("entry")), _fnum(card.get("sl")), _fnum(card.get("tp"))
    if entry is None or sl is None or tp is None or entry <= 0 or sl <= 0 or tp <= 0:
        reasons.append("entry/sl/tp 须>0")
    elif len({entry, sl, tp}) < 3:
        reasons.append("entry/sl/tp 须互异")
    else:
        d_sl, d_tp = abs(sl - entry) / entry, abs(tp - entry) / entry
        if not (DELTA_MIN <= d_sl <= DELTA_MAX):
            reasons.append("|sl-entry|/entry 须∈[0.5%,8%]")
        if not (DELTA_MIN <= d_tp <= DELTA_MAX):
            reasons.append("|tp-entry|/entry 须∈[0.5%,8%]")
        side = str(card.get("side") or "")
        if side == "LONG" and not (sl < entry < tp):
            reasons.append("LONG 须 sl<entry<tp")
        if side == "SHORT" and not (tp < entry < sl):
            reasons.append("SHORT 须 tp<entry<sl")
    lev = _fnum(card.get("lev"))
    if lev is None or not (LEV_MIN <= lev <= LEV_MAX) or int(lev) != lev:
        reasons.append("lev 须∈[5,10]")
    margin = _fnum(card.get("margin"), MARGIN)
    if margin != MARGIN:
        reasons.append("margin 须=30")
    if st.get("open_position"):
        reasons.append("单仓制")
    sym = str(card.get("symbol") or "")
    if not sym:
        reasons.append("缺 symbol")
    else:
        try:
            market._check_symbol(sym)
        except ValueError:
            reasons.append("symbol 格式非法(模型幻觉)")
        if sym in _cooldown_symbols():
            reasons.append("symbol 24h 冷却")
    return reasons


def gate_node(state: GraphState) -> dict:
    card = state.get("scout_card") or {}
    tv = state.get("trader_verdict") or {}
    scout_v = str(card.get("verdict") or "")
    if scout_v in ("WATCH", "NO_CANDIDATE", "DATA_GAP"):
        action = "NO_TRADE" if scout_v == "NO_CANDIDATE" else scout_v
        return {"gate_ok": False, "gate_reason": f"scout:{scout_v}", "action": action}
    if state.get("action") == "NO_TRADE":
        return {"gate_ok": False, "gate_reason": state.get("gate_reason") or "NO_TRADE", "action": "NO_TRADE"}

    st = ledger.load_state()
    reasons = _gate_reasons(card, st)
    if tv.get("would_block") is True:
        action = str(tv.get("verdict") or "GATE_BLOCK")
        return {"gate_ok": False, "gate_reason": ";".join(reasons) or "would_block", "action": action}
    if reasons:
        return {"gate_ok": False, "gate_reason": ";".join(reasons), "action": "GATE_BLOCK"}
    return {"gate_ok": True, "gate_reason": "ok", "action": "SIM_OPEN"}


def after_gate(state: GraphState) -> str:
    if state.get("gate_ok") and str(state.get("action") or "") == "SIM_OPEN":
        return "final_review"
    return "execute"


def _review_quota(st: dict) -> tuple[int, str]:
    today = _now().strftime("%Y-%m-%d")
    date = str(st.get("final_review_date") or "")
    if date != today:
        return 0, today
    try:
        n = int(st.get("final_reviews_today") or 0)
    except (TypeError, ValueError):
        n = 0
    return n, today


def final_review_node(state: GraphState) -> dict:
    """仅 gate 全过且即将 SIM_OPEN 时到达。chat_astra 抛异常 → timeout 交给 price_check。"""
    errors: list[str] = []
    st = ledger.load_state()
    n, today = _review_quota(st)
    st["final_review_date"] = today
    if n >= FINAL_REVIEW_DAILY_CAP:
        st["final_reviews_today"] = n
        ledger.save_state(st)
        errors.append("final_review 日限额已满")
        return {
            "action": "FINAL_REVIEW_REJECT",
            "final_review_timeout": False,
            "errors": errors,
        }

    card = _public_card(state.get("scout_card") or {})
    symbol = str(card.get("symbol") or "")
    mark = None
    try:
        if symbol:
            mark = market.mark_price(symbol)
    except Exception as e:  # noqa: BLE001
        errors.append(f"final_review mark: {e}")

    system = (
        "你是开仓终审官。只输出严格 JSON："
        '{"approve":true|false,"理由":"≤60字"}。不要 Markdown，不要其它字段。'
    )
    user = (
        f"# 已批研究卡\n{json.dumps(card, ensure_ascii=False)}\n"
        f"# Adversary\n{json.dumps(state.get('adv_verdict'), ensure_ascii=False)}\n"
        f"# 闸门\n{json.dumps({'ok': state.get('gate_ok'), 'reason': state.get('gate_reason'), 'action': state.get('action')}, ensure_ascii=False)}\n"
        f"# 行情摘要\n{state.get('material') or ''}\n"
        f"# 当前 mark\n{mark}\n"
    )

    st["final_reviews_today"] = n + 1
    ledger.save_state(st)

    try:
        raw, channel = llm.chat_astra(system, user)
    except Exception as e:  # noqa: BLE001
        errors.append(f"final_review 超时/失败: {e}")
        return {"final_review_timeout": True, "errors": errors}

    st = ledger.load_state()
    st["astra_channel"] = channel
    ledger.save_state(st)

    try:
        obj = extract_json(raw)
    except ValueError as e:
        errors.append(f"final_review JSON 失败: {e}")
        return {"action": "FINAL_REVIEW_REJECT", "final_review_timeout": False, "errors": errors}

    if obj.get("approve") is True:
        return {"final_review_timeout": False, "errors": errors}
    if obj.get("approve") is False:
        errors.append(f"final_review 拒绝: {obj.get('理由') or ''}")
        return {"action": "FINAL_REVIEW_REJECT", "final_review_timeout": False, "errors": errors}
    errors.append("final_review approve 非 bool")
    return {"action": "FINAL_REVIEW_REJECT", "final_review_timeout": False, "errors": errors}


def after_final_review(state: GraphState) -> str:
    if str(state.get("action") or "") == "FINAL_REVIEW_REJECT":
        return "execute"
    return "price_check"


def price_check_node(state: GraphState) -> dict:
    """execute 前最后一道。终审 timeout 也经此定夺：价仍在则放行 SIM_OPEN。"""
    errors: list[str] = []
    card = dict(state.get("scout_card") or {})
    entry = _fnum(card.get("entry"))
    sl = _fnum(card.get("sl"))
    tp = _fnum(card.get("tp"))
    symbol = str(card.get("symbol") or "")
    if entry is None or entry <= 0 or not symbol:
        errors.append("price_check 缺 entry/symbol")
        return {"action": "PRICE_MOVED", "errors": errors}
    try:
        mark = float(market.mark_price(symbol))
    except Exception as e:  # noqa: BLE001
        errors.append(f"price_check mark: {e}")
        return {"action": "PRICE_MOVED", "errors": errors}
    if mark <= 0:
        errors.append("price_check mark<=0")
        return {"action": "PRICE_MOVED", "errors": errors}
    if abs(mark - entry) / entry > PRICE_DEV_MAX:
        return {"action": "PRICE_MOVED", "errors": errors}
    shift = mark - entry
    card["entry"] = mark
    if sl is not None:
        card["sl"] = sl + shift
    if tp is not None:
        card["tp"] = tp + shift
    return {"scout_card": card, "action": "SIM_OPEN", "errors": errors}


def execute_node(state: GraphState) -> dict:
    action = str(state.get("action") or "NO_TRADE")
    card = dict(state.get("scout_card") or {})
    card.setdefault("margin", MARGIN)
    rnd = int(state.get("round") or 0)
    st = ledger.load_state()
    if action == "SIM_OPEN":
        pos = {
            "symbol": str(card.get("symbol") or ""),
            "side": str(card.get("side") or ""),
            "entry": float(card["entry"]),
            "sl": float(card["sl"]),
            "tp": float(card["tp"]),
            "lev": int(float(card["lev"])),
            "margin": float(card.get("margin", MARGIN)),
            "opened_at": ledger.now_utc(),
            "round_opened": rnd,
        }
        st["open_position"] = pos
        ledger.save_state(st)
        _journal("SIM_OPEN", rnd, {**card, "margin": pos["margin"]}, by="Trader")
    else:
        _journal(action, rnd, card, by="Trader")
    return {}


def _score_item(ok: bool, deduct: int, acc: list[int]) -> None:
    if not ok:
        acc[0] = max(0, acc[0] - deduct)


def _clip(text: Any, n: int) -> str:
    s = str(text or "")
    return s if len(s) <= n else s[:n] + "…"


def _code_grade(state: GraphState) -> dict:
    """规则评分 fallback：字段/数值纪律核对。无问题时是 10/10/10。"""
    card = state.get("scout_card") or {}
    adv = state.get("adv_verdict")
    tv = state.get("trader_verdict") or {}
    action = str(state.get("action") or "")
    hard: list[str] = []
    soft: list[str] = []
    research, risk, discipline = [10], [10], [10]

    missing_s = [k for k in SCOUT_REQUIRED if k not in card]
    if missing_s:
        hard.append("Scout缺字段:" + ",".join(missing_s))
        _score_item(False, 4, discipline)
        _score_item(False, 3, research)
    if card.get("verdict") == "CANDIDATE" and not str(card.get("理由") or "").strip():
        soft.append("CANDIDATE无理由")
        _score_item(False, 2, research)

    lev = _fnum(card.get("lev"))
    if card.get("verdict") == "CANDIDATE" and (lev is None or not (LEV_MIN <= lev <= LEV_MAX)):
        hard.append("lev超界")
        _score_item(False, 4, discipline)
        _score_item(False, 3, risk)

    margin = _fnum(card.get("margin"), MARGIN)
    if action == "SIM_OPEN" and margin != MARGIN:
        hard.append("margin!=30")
        _score_item(False, 4, discipline)

    if card.get("_redone") and int(_fnum(card.get("rev"), 0) or 0) < 1:
        hard.append("rev未+1")
        _score_item(False, 3, discipline)

    if card.get("verdict") == "CANDIDATE" and not state.get("adv_used"):
        hard.append("CANDIDATE未送审")
        _score_item(False, 4, discipline)

    if state.get("adv_used"):
        if not isinstance(adv, dict) or "pass" not in adv:
            hard.append("Adv缺pass")
            _score_item(False, 3, discipline)

    missing_t = [k for k in TRADER_REQUIRED if k not in tv] if tv else list(TRADER_REQUIRED)
    if action != "NO_TRADE" or tv:
        if missing_t and action not in ("NO_TRADE",):
            # Adv 二杀直达 execute 时可能没有 trader_verdict，不当 hard
            if tv:
                hard.append("Trader缺字段:" + ",".join(missing_t))
                _score_item(False, 3, discipline)

    if action == "SIM_OPEN":
        reasons = _gate_reasons(card, {"open_position": None})
        # 开仓后 state 已有仓，闸门复检用无仓视图只核数值
        num_fail = [r for r in reasons if r not in ("单仓制",)]
        if num_fail:
            hard.append("开仓数值不合规:" + ";".join(num_fail))
            _score_item(False, 4, risk)

    return {
        "research": research[0],
        "risk": risk[0],
        "discipline": discipline[0],
        "hard_fail": hard,
        "soft_fail": soft,
        "评语": "",
    }


def _score10(v: Any) -> int | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    if n < 1 or n > 10:
        return None
    return n


def _parse_model_grade(obj: dict) -> dict | None:
    if not isinstance(obj, dict):
        return None
    for k in ("research", "risk", "discipline", "hard_fail", "soft_fail", "评语"):
        if k not in obj:
            return None
    research, risk, discipline = _score10(obj["research"]), _score10(obj["risk"]), _score10(obj["discipline"])
    if research is None or risk is None or discipline is None:
        return None
    return {
        "research": research,
        "risk": risk,
        "discipline": discipline,
        "hard_fail": obj.get("hard_fail"),
        "soft_fail": obj.get("soft_fail"),
        "评语": _clip(obj.get("评语"), 60),
    }


def _hard_fail_list(grade: dict) -> list[str]:
    hf = grade.get("hard_fail")
    if isinstance(hf, list):
        return [str(x) for x in hf if str(x).strip()]
    s = str(hf or "").strip()
    if not s or s in ("无", "无或描述", "none", "None"):
        return []
    return [s]


def _grade_prompt(state: GraphState) -> tuple[str, str]:
    card = _public_card(state.get("scout_card") or {})
    system = (
        "你是阅卷官。只输出严格 JSON，不要 Markdown，不要其它字段："
        '{"research":1-10,"risk":1-10,"discipline":1-10,'
        '"hard_fail":"无或描述","soft_fail":"无或描述","评语":"点名具体行为≤60字"}。'
        "评分必须有区分度：WATCH 理由平庸（套话、无证伪条件、未点名数据）research 就该≤6；"
        "该扣分就扣，不许和稀泥，无故 10/10/10 视为失职。"
        "点名具体行为（谁、做了什么），不要空话。"
    )
    user = (
        f"# 题目类型\n{state.get('topic_type')}\n"
        f"# 材料摘要\n{_clip(state.get('material'), 800)}\n"
        f"# Scout 输出\n{json.dumps(card, ensure_ascii=False)}\n"
        f"# Adversary 输出\n{json.dumps(state.get('adv_verdict'), ensure_ascii=False)}\n"
        f"# Trader 输出\n{json.dumps(state.get('trader_verdict') or {}, ensure_ascii=False)}\n"
        f"# 最终 action\n{state.get('action')}\n"
        f"# 闸门/价格结果\n{json.dumps({'gate_ok': state.get('gate_ok'), 'gate_reason': state.get('gate_reason'), 'entry': card.get('entry'), 'final_review_timeout': state.get('final_review_timeout')}, ensure_ascii=False)}\n"
    )
    return system, user


def grade_node(state: GraphState) -> dict:
    errors: list[str] = []
    grade = None
    system, user = _grade_prompt(state)
    try:
        raw = llm.chat("glm", system, user)
        grade = _parse_model_grade(extract_json(raw))
        if grade is None:
            errors.append("grade 模型输出不合格，回退代码评分")
    except Exception as e:  # noqa: BLE001
        errors.append(f"grade 模型失败: {e}")
        grade = None
    if grade is None:
        grade = _code_grade(state)

    card = state.get("scout_card") or {}
    adv = state.get("adv_verdict")
    tv = state.get("trader_verdict") or {}
    score = f"{grade.get('research')}/{grade.get('risk')}/{grade.get('discipline')}"
    _journal("GRADE", int(state.get("round") or 0), {**card, "margin": card.get("margin", MARGIN)},
             score=score, by="grade")

    hard = _hard_fail_list(grade)
    if hard:
        cur = _load_curriculum()
        rq = list(cur.get("retest_queue") or [])
        rq.append({
            "round": state.get("round"),
            "topic_type": state.get("topic_type"),
            "symbol": card.get("symbol"),
            "fails": hard,
        })
        cur["retest_queue"] = rq
        _save_curriculum(cur)

    learned = dict(state.get("learned") or {})
    if card.get("learned") and "Scout" not in learned:
        learned["Scout"] = str(card["learned"])
    if isinstance(adv, dict) and adv.get("learned") and "Adversary" not in learned:
        learned["Adversary"] = str(adv["learned"])
    if tv.get("learned") and "Trader" not in learned:
        learned["Trader"] = str(tv["learned"])
    return {"grade": grade, "learned": learned, "errors": errors}


def _playing_roles(state: GraphState) -> list[str]:
    roles = ["Scout"]
    if state.get("adv_used"):
        roles.append("Adversary")
    roles.append("Trader")
    return roles


def _round_record(state: GraphState, role: str) -> str:
    card = _public_card(state.get("scout_card") or {})
    own = {
        "Scout": card,
        "Adversary": state.get("adv_verdict"),
        "Trader": state.get("trader_verdict") or {},
    }.get(role)
    rec = {
        "topic_type": state.get("topic_type"),
        "topic": state.get("topic"),
        "material": _clip(state.get("material"), 800),
        "own_output": own,
        "adv_verdict": state.get("adv_verdict"),
        "action": state.get("action"),
        "gate_reason": state.get("gate_reason"),
        "grade": state.get("grade") or {},
    }
    return json.dumps(rec, ensure_ascii=False)


def reflect_node(state: GraphState) -> dict:
    """各上场岗位用自身模型自我反思；learned 走现有 log 机制，patch 交给 apply_patch。"""
    errors: list[str] = []
    learned = dict(state.get("learned") or {})
    reflects: dict[str, dict] = {}
    for role in _playing_roles(state):
        profile, _log = ledger.load_memory(role)
        prompt = (
            "# 自我反思\n"
            "阅读你的岗位 profile 与本轮完整记录，只输出严格 JSON（不要 Markdown，不要其它字段）：\n"
            '{"learned":"≤40字","profile_patch":null或'
            '{"section":"要改的段名","op":"append或revise","content":"新增或替换的条目文本","reason":"≤40字"}}\n'
            "约束：learned 必填；多数轮 profile_patch 应为 null；"
            "禁止改「输出格式」段；禁止削弱风控；禁止出现提高杠杆/放大仓位/降低门槛/跳过审核/无视。\n"
            f"# 你的 profile 全文\n{profile}\n"
            f"# 本轮完整记录\n{_round_record(state, role)}\n"
        )
        try:
            raw = ask_role(role, prompt, memory_limit=0)
            obj = extract_json(raw)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{role} reflect 失败: {e}")
            continue
        reflects[role] = obj
        text = str(obj.get("learned") or "").strip()
        if text:
            learned[role] = _clip(text, 40)
    return {"reflects": reflects, "learned": learned, "errors": errors}


def _log_engine_error(msg: str) -> None:
    errp = ledger.RUNTIME / "errors.log"
    with errp.open("a", encoding="utf-8") as f:
        f.write(f"{ledger.now_utc()} {msg}\n")


def _reject_patch(role: str, why: str) -> str:
    msg = f"apply_patch 拒绝 {role}: {why}"
    _log_engine_error(msg)
    return msg


def _load_patch_times() -> dict:
    if not PATCH_TIMES.exists():
        return {}
    try:
        obj = json.loads(PATCH_TIMES.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_patch_times(times: dict) -> None:
    PATCH_TIMES.parent.mkdir(parents=True, exist_ok=True)
    PATCH_TIMES.write_text(json.dumps(times, ensure_ascii=False, indent=2), encoding="utf-8")


def _split_sections(text: str) -> tuple[str, list[dict[str, str]]]:
    lines = str(text or "").splitlines()
    idx = [i for i, ln in enumerate(lines) if ln.startswith("## ")]
    pre_end = idx[0] if idx else len(lines)
    preamble = "\n".join(lines[:pre_end])
    sections: list[dict[str, str]] = []
    for n, start in enumerate(idx):
        end = idx[n + 1] if n + 1 < len(idx) else len(lines)
        heading = lines[start]
        sections.append({
            "heading": heading,
            "name": heading.lstrip("#").strip(),
            "body": "\n".join(lines[start + 1 : end]),
        })
    return preamble, sections


def _rebuild_profile(preamble: str, sections: list[dict[str, str]]) -> str:
    parts: list[str] = []
    if preamble.strip():
        parts.append(preamble.rstrip("\n") + "\n")
    for sec in sections:
        chunk = sec["heading"]
        body = (sec.get("body") or "").rstrip("\n")
        if body:
            chunk += "\n" + body
        parts.append(chunk)
    return "\n".join(parts).rstrip("\n") + "\n"


def _find_section(sections: list[dict[str, str]], want: str) -> dict[str, str] | None:
    want = str(want or "").strip().lstrip("#").strip()
    if not want:
        return None
    exact = [s for s in sections if s["name"] == want]
    if exact:
        return exact[0]
    hit = [s for s in sections if want in s["name"] or s["name"] in want]
    if not hit:
        return None
    hit.sort(key=lambda s: len(s["name"]))
    return hit[0]


def _normalize_patch(raw: Any) -> dict | None | str:
    """None=无需改；dict=待应用；str=非法说明。"""
    if raw is None or raw is False or raw == "" or raw == "null":
        return None
    if not isinstance(raw, dict):
        return "profile_patch 不是对象"
    if not raw:
        return None
    return raw


def _debounced(role: str, section: str) -> bool:
    key = f"{role}:{section}"
    ts = _load_patch_times().get(key)
    t = _parse_ts(str(ts or ""))
    if t is None:
        return False
    return (_now() - t) < PATCH_DEBOUNCE


def apply_patch(role: str, patch: dict | None) -> str | None:
    """安全闸：通过则写回 profile.md。成功/无需改返回 None；拒绝返回原因（已记 errors.log）。"""
    if role not in ALLOWED_ROLES:
        return _reject_patch(str(role), "非法角色")
    norm = _normalize_patch(patch)
    if isinstance(norm, str):
        return _reject_patch(role, norm)
    if norm is None:
        return None
    section = str(norm.get("section") or "").strip()
    op = str(norm.get("op") or "").strip().lower()
    content = str(norm.get("content") or "")
    reason = _clip(norm.get("reason"), 40)
    if "输出格式" in section:
        return _reject_patch(role, "禁止修改输出格式段")
    if op not in ("append", "revise"):
        return _reject_patch(role, f"非法 op:{op}")
    if not section:
        return _reject_patch(role, "缺 section")
    if not str(content).strip():
        return _reject_patch(role, "缺 content")
    blob = json.dumps(norm, ensure_ascii=False)
    if BAD_PATCH_RE.search(blob):
        return _reject_patch(role, "含提高杠杆/放大仓位/降低门槛/跳过审核/无视 类表述")

    path = ledger.MEMORY / role / "profile.md"
    if not path.exists():
        return _reject_patch(role, "profile.md 不存在")
    original = path.read_text(encoding="utf-8")
    preamble, sections = _split_sections(original)
    sec = _find_section(sections, section)
    if sec is None:
        return _reject_patch(role, f"无此段:{section}")
    if "输出格式" in sec["name"]:
        return _reject_patch(role, "禁止修改输出格式段")
    if _debounced(role, sec["name"]):
        return _reject_patch(role, f"同一段 24h 内已改过:{sec['name']}")

    old_body = sec.get("body") or ""
    if op == "append":
        line = " ".join(content.splitlines()).strip()
        if not line.startswith("- "):
            line = "- " + line
        new_body = old_body.rstrip("\n")
        new_body = (new_body + "\n" if new_body else "") + line
    else:
        new_body = content.strip("\n")
        deleted = old_body
        if RISK_KEEP_RE.search(deleted):
            lost = set(RISK_KEEP_RE.findall(deleted)) - set(RISK_KEEP_RE.findall(new_body))
            if lost:
                return _reject_patch(role, "削弱风控关键词:" + ",".join(sorted(lost)))
    sec["body"] = new_body
    path.write_text(_rebuild_profile(preamble, sections), encoding="utf-8")
    times = _load_patch_times()
    times[f"{role}:{sec['name']}"] = ledger.now_utc()
    _save_patch_times(times)
    ledger.append_log(role, f"profile 自改: {op} {sec['name']} {reason}")
    return None


def apply_patch_node(state: GraphState) -> dict:
    errors: list[str] = []
    reflects = state.get("reflects") or {}
    for role in _playing_roles(state):
        obj = reflects.get(role) or {}
        err = apply_patch(role, obj.get("profile_patch") if isinstance(obj, dict) else None)
        if err:
            errors.append(err)
    return {"errors": errors}


def _empty(rnd: int) -> GraphState:
    return {
        "round": int(rnd),
        "topic_type": "",
        "topic": "",
        "material": "",
        "scout_card": {},
        "adv_verdict": None,
        "adv_used": False,
        "trader_verdict": {},
        "gate_ok": False,
        "gate_reason": "",
        "action": "",
        "grade": {},
        "learned": {},
        "errors": [],
        "adv_just_ran": False,
        "final_review_timeout": False,
        "reflects": {},
    }


def build_app():
    g = StateGraph(GraphState)
    g.add_node("coach", coach_node)
    g.add_node("scout", scout_node)
    g.add_node("adv_trader", adv_trader_node)
    g.add_node("gate", gate_node)
    g.add_node("final_review", final_review_node)
    g.add_node("price_check", price_check_node)
    g.add_node("execute", execute_node)
    g.add_node("grade", grade_node)
    g.add_node("reflect", reflect_node)
    g.add_node("apply_patch", apply_patch_node)
    g.add_edge(START, "coach")
    g.add_edge("coach", "scout")
    g.add_edge("scout", "adv_trader")
    g.add_conditional_edges(
        "adv_trader", after_adv_trader,
        {"scout": "scout", "gate": "gate", "execute": "execute"},
    )
    g.add_conditional_edges(
        "gate", after_gate,
        {"final_review": "final_review", "execute": "execute"},
    )
    g.add_conditional_edges(
        "final_review", after_final_review,
        {"price_check": "price_check", "execute": "execute"},
    )
    g.add_edge("price_check", "execute")
    g.add_edge("execute", "grade")
    g.add_edge("grade", "reflect")
    g.add_edge("reflect", "apply_patch")
    g.add_edge("apply_patch", END)
    return g.compile()


app = build_app()


def invoke(partial: dict | None = None) -> dict:
    st = ledger.load_state()
    init = _empty(st.get("round", 0))
    if partial:
        for k, v in partial.items():
            init[k] = v  # type: ignore[literal-required]
    return app.invoke(init, {"recursion_limit": 25})
