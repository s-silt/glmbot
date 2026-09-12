# -*- coding: utf-8 -*-
"""llm.py —— 统一模型调用层：akile(OpenAI兼容) / bigmodel(Anthropic兼容) 。
key 从桌面 api.txt / api2.txt 按组名读取，绝不打印。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

API_FILES = [Path(r"C:\Users\sxl\Desktop\api.txt"), Path(r"C:\Users\sxl\Desktop\api2.txt")]

# 模型注册表：alias -> (provider, model_id, 组名)
MODELS = {
    "astra":  ("akile",     "gpt-6-astra",      "gpt-6-astra"),
    "fable":  ("akile",     "claude-fable-5-1", "claude-fable-5-1"),
    "glm":    ("bigmodel",  "glm-5.3",          "glm-5.3-flash/glm-5.3"),
    "flash":  ("bigmodel",  "glm-5.3-flash",    "glm-5.3-flash/glm-5.3"),
}


def _codex_chat(model_id: str, system: str, user: str, timeout: int = 360) -> str:
    """经本机 Codex CLI 调 astra（走 ChatGPT 订阅额度）。失败抛异常由上层 fallback。
    -o last-message 写临时文件精确取回模型回答。"""
    import tempfile
    # npm 的 cmd shim 会吃掉参数里的换行，压成单行传递
    oneline = lambda s: " ".join(s.split())
    prompt = f"[系统设定] {oneline(system)} [任务] {oneline(user)}"
    import shutil
    codex_bin = shutil.which("codex") or shutil.which("codex.cmd")
    if not codex_bin:
        raise RuntimeError("codex cli 不在 PATH")
    fd, path = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    try:
        r = subprocess.run(
            [codex_bin, "exec", "--skip-git-repo-check", "-o", path, "-m", model_id, prompt],
            capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
            cwd=os.environ.get("TEMP", "."))
        answer = Path(path).read_text(encoding="utf-8", errors="replace").strip()
        if answer:
            return answer
        out = (r.stdout or "").strip()
        if r.returncode != 0 or not out:
            raise RuntimeError(f"codex exec rc={r.returncode}: {(r.stderr or '')[:200]}")
        lines = [l for l in out.splitlines() if l.strip()]
        while lines and (lines[-1].replace(",", "").isdigit() or lines[-1].startswith(("tokens used", "hook:"))):
            lines.pop()
        return "\n".join(lines).strip() or out
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def chat_astra(system: str, user: str, long_task: bool = False) -> tuple[str, str]:
    """astra 调用。默认开仓终审档（时限敏感：codex 45s → API 25s，绝不无限等）；
    long_task=True 长任务档（架构复核等：codex 300s → API 90s）。返回 (回答, 通道)。"""
    ct, at = (300, 90) if long_task else (45, 25)
    try:
        return _codex_chat("gpt-6-astra", system, user, timeout=ct), "codex"
    except Exception:  # noqa: BLE001
        return chat("astra", system, user, timeout=at, retries=1), "api"


def _load_group(group: str) -> tuple[str, str]:
    """按组名行在两个 api 文件里定位 (key, base_url)。组名行下一行是 key。"""
    for f in API_FILES:
        if not f.exists():
            continue
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, line in enumerate(lines):
            s = line.strip()
            if s == group or s.startswith(group + "/"):
                key, base = "", ""
                for follow in lines[i + 1:]:
                    t = follow.strip()
                    if t.startswith("base_url"):
                        base = t.split("=", 1)[1].split("#", 1)[0].strip().strip('"').strip()
                        if key:
                            return key, base
                    elif t and not t.startswith("#") and " " not in t and "http" not in t and len(t) > 20 and "/" not in t:
                        key = t
                    elif not t and key:
                        break
                if key and base:
                    return key, base
    raise RuntimeError(f"api 文件中未找到组 {group}")


def chat(alias: str, system: str, user: str, max_tokens: int = 4096, timeout: int = 180, retries: int = 1) -> str:
    """调一次模型，返回纯文本。失败重试 1 次。fable/akile 不带 temperature(网关400)。"""
    provider, model_id, group = MODELS[alias]
    key, base = _load_group(group)
    for attempt in range(1, retries + 2):
        try:
            if provider == "akile":
                url = base.rstrip("/") + "/v1/chat/completions"
                payload = {"model": model_id, "max_tokens": max_tokens,
                           "messages": [{"role": "system", "content": system},
                                        {"role": "user", "content": user}]}
                headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json",
                           "User-Agent": "GLMBOT-engine/1.0"}
            else:  # bigmodel anthropic 端点（Coding Plan 额度）
                url = base.rstrip("/") + "/v1/messages"
                payload = {"model": model_id, "max_tokens": max_tokens,
                           "system": system,
                           "messages": [{"role": "user", "content": user}]}
                headers = {"x-api-key": key, "Authorization": "Bearer " + key,
                           "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
            req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
            resp = json.load(urllib.request.urlopen(req, timeout=timeout))
            if provider == "akile":
                return resp["choices"][0]["message"]["content"].strip()
            return "\n".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text").strip()
        except Exception as e:  # noqa: BLE001
            if attempt > retries:
                raise RuntimeError(f"llm.chat({alias}) 失败({attempt}次): {e}") from e
            time.sleep(20)
    return ""  # unreachable
