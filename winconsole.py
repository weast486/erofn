"""윈도우 검은 창(콘솔)의 '빠른 편집 모드' 끄기.

창 안을 마우스로 클릭하면 제목에 '선택' 이 붙고, 그동안 화면에 글자를 쓰려는 프로그램이 멈춘다
(2026-10-13: 키움 봇 창이 이 상태로 멈춰 08:40 시작을 못 함). 봇을 켤 때 이 모드를 꺼서 클릭해도 안 멈추게 한다.
"""
from __future__ import annotations

import sys


def disable_quick_edit() -> bool:
    """꺼졌으면 True. 윈도우가 아니거나 콘솔이 없으면 아무것도 안 하고 False."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        new = (mode.value & ~0x0040) | 0x0080  # QUICK_EDIT 끄기 + EXTENDED_FLAGS(있어야 적용됨)
        return bool(k.SetConsoleMode(h, new))
    except Exception:  # noqa: BLE001
        return False
