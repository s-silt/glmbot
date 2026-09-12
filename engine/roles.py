# -*- coding: utf-8 -*-
"""roles.py —— 岗位调用与 JSON 抽取。"""
from __future__ import annotations

import json
import re
import threading

import ledger
import llm

ROLE_MODELS = {"Scout": "flash", "Adversary": "glm", "Trader": "flash"}

# mock.py 按角色返回预设 JSON；Scout/Trader 现共用 flash，且 Adv∥Trader 并行，必须 thread-local
_tls = threading.local()


def current_role() -> str | None:
    return getattr(_tls, "role", None)


def ask_role(role: str, task_prompt: str, memory_limit: int = 15) -> str:
    """注入该角色 profile + 近期 log，调用对应模型。system=profile，log 拼进 user。"""
    profile, log = ledger.load_memory(role)
    if log:
        lines = log.splitlines()
        if memory_limit is not None:
            lines = lines[-int(memory_limit) :]
        log = "\n".join(lines)
        user = f"## 近期 log\n{log}\n\n## 任务\n{task_prompt}"
    else:
        user = task_prompt
    prev = getattr(_tls, "role", None)
    _tls.role = role
    try:
        return llm.chat(ROLE_MODELS[role], profile or f"你是{role}。", user)
    finally:
        _tls.role = prev


def extract_json(text: str) -> dict:
    """剥 ```json 围栏、解析首个 {...}。失败抛 ValueError。"""
    if text is None:
        raise ValueError("extract_json: empty")
    s = str(text).strip()
    if not s:
        raise ValueError("extract_json: empty")
    if "```" in s:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", s, flags=re.IGNORECASE)
        if m:
            s = m.group(1).strip()
        else:
            s = s.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
    i = s.find("{")
    if i < 0:
        raise ValueError("extract_json: no object")
    try:
        obj, _end = json.JSONDecoder().raw_decode(s[i:])
    except json.JSONDecodeError as e:
        raise ValueError(f"extract_json: {e}") from e
    if not isinstance(obj, dict):
        raise ValueError("extract_json: not a dict")
    return obj
