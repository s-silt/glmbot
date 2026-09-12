# -*- coding: utf-8 -*-
"""loop.py —— 常驻循环：每 INTERVAL 秒跑一轮。Ctrl+C 干净退出。"""
from __future__ import annotations

import os
import time

import mock as glmbot_mock
glmbot_mock.apply()

import ledger
import run_once


def _halted(st: dict) -> bool:
    try:
        br = float(st.get("bankroll") or 0)
    except (TypeError, ValueError):
        br = 0.0
    return br <= 0 or st.get("halt") is True


def main() -> None:
    interval = float(os.environ.get("GLMBOT_INTERVAL", "1800"))
    try:
        while True:
            st = ledger.load_state()
            if _halted(st):
                print(f"停机 bankroll={st.get('bankroll')} halt={st.get('halt')}")
                return
            run_once.run()
            st = ledger.load_state()
            if _halted(st):
                print(f"停机 bankroll={st.get('bankroll')} halt={st.get('halt')}")
                return
            time.sleep(interval)
    except KeyboardInterrupt:
        print("已退出")


if __name__ == "__main__":
    main()
