# -*- coding: utf-8 -*-
"""ledger.py —— 影子账本与状态读写。所有写操作只碰 runtime\\ 目录。"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

RUNTIME = Path(r"E:\GLMBOT\runtime")
STATE = RUNTIME / "state.json"
LEDGER = RUNTIME / "shadow_ledger.md"
MEMORY = RUNTIME / "memory"


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8"))


def save_state(st: dict) -> None:
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def append_journal(cols: list[str]) -> None:
    """按账本表头列数追加一行（调用方保证顺序）。"""
    line = "| " + " | ".join(str(c) for c in cols) + " |"
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def pnl(side: str, entry: float, exit_: float, lev: int, margin: float) -> float:
    m = 1.0 if side == "LONG" else -1.0
    return (exit_ - entry) / entry * lev * margin * m


def load_memory(role: str) -> tuple[str, str]:
    """(profile 正文, 近期 log 摘要 ≤15 行)。"""
    p = MEMORY / role / "profile.md"
    profile = p.read_text(encoding="utf-8") if p.exists() else ""
    log_file = MEMORY / role / "log-2026-09.md"
    log = ""
    if log_file.exists():
        lines = log_file.read_text(encoding="utf-8").splitlines()
        log = "\n".join(lines[-15:])
    return profile, log


def append_log(role: str, text: str) -> None:
    d = MEMORY / role
    d.mkdir(parents=True, exist_ok=True)
    lf = d / "log-2026-09.md"
    stamp = datetime.now(timezone.utc).strftime("%m-%d %H:%M")
    with lf.open("a", encoding="utf-8") as f:
        f.write(f"- {stamp} {text}\n")
