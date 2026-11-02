"""Windows command-line quoting helpers (offline validation only)."""
from __future__ import annotations

import subprocess
import sys


def quote_windows_argument(text: str) -> str:
    if text == "":
        return '""'
    if not any(ch in text for ch in ' \t"'):
        return text
    out = ['"']
    i = 0
    while i < len(text):
        backslashes = 0
        while i < len(text) and text[i] == "\\":
            backslashes += 1
            i += 1
        if i >= len(text):
            out.append("\\" * (2 * backslashes))
            break
        if text[i] == '"':
            out.append("\\" * (2 * backslashes + 1))
            out.append('"')
            i += 1
        else:
            if backslashes:
                out.append("\\" * backslashes)
            out.append(text[i])
            i += 1
    out.append('"')
    return "".join(out)


def join_windows_cmdline(args: list[str]) -> str:
    return " ".join(quote_windows_argument(arg) for arg in args)


def matches_windows_list2cmdline(args: list[str]) -> bool:
    if sys.platform != "win32":
        return True
    return join_windows_cmdline(args) == subprocess.list2cmdline(args)
