"""A conservative, dependency-free token estimate.

Hangul and CJK characters are usually one token each; Latin text is closer to
four characters per token. Overestimating is the safe direction for prompt
caps.
"""

from __future__ import annotations

import re

_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7a3]")


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    return cjk + max(1, (other + 3) // 4)
