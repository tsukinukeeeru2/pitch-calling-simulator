"""ターミナルの色付けヘルパー。

パイプ・リダイレクト・NO_COLOR・TERM=dumb のときは自動で無効になる
(テストや `python main.py > log.txt` を汚さない)。
PITCHSIM_COLOR=1 で強制有効(デモ・スクリーンショット用)。
"""

import os
import re
import sys

_CODES = {
    "reset": "0", "bold": "1", "dim": "2", "invert": "7",
    "red": "31", "green": "32", "yellow": "33", "blue": "34",
    "magenta": "35", "cyan": "36", "white": "37", "grey": "90",
    "bred": "91", "bgreen": "92", "byellow": "93", "bblue": "94",
    "bmagenta": "95", "bcyan": "96",
    "on_blue": "44", "on_grey": "100", "on_red": "41", "on_green": "42",
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def enabled():
    if os.environ.get("PITCHSIM_COLOR") == "1":
        return True
    if os.environ.get("NO_COLOR") is not None or os.environ.get("TERM") == "dumb":
        return False
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def paint(text, *styles):
    if not styles or not enabled():
        return str(text)
    seq = ";".join(_CODES[s] for s in styles if s in _CODES)
    return f"\x1b[{seq}m{text}\x1b[0m"


def strip(text):
    """色コードを取り除く(幅計算・テスト用)。"""
    return _ANSI_RE.sub("", text)


# よく使う短縮
def bold(t):
    return paint(t, "bold")


def dim(t):
    return paint(t, "dim")


def red(t):
    return paint(t, "bred")


def green(t):
    return paint(t, "bgreen")


def yellow(t):
    return paint(t, "byellow")


def cyan(t):
    return paint(t, "bcyan")


def blue(t):
    return paint(t, "bblue")


def magenta(t):
    return paint(t, "bmagenta")


def grey(t):
    return paint(t, "grey")
