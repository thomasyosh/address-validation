from __future__ import annotations

import re

CJK_PATTERN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")
LATIN_PATTERN = re.compile(r"[A-Za-z]")


def is_chinese_address(address: str) -> bool:
    """True only for Chinese-only addresses. Mixed Chinese/English counts as English."""
    has_cjk = bool(CJK_PATTERN.search(address))
    has_latin = bool(LATIN_PATTERN.search(address))
    return has_cjk and not has_latin
