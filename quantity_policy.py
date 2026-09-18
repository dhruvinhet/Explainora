"""Topic-independent policy for quantitative literals in generated lessons."""

from __future__ import annotations

import re
from typing import Any, List


QUANTITY_RE = re.compile(
    r"(?<![\w.])(?:[$€£₹]\s*)?[+-]?(?:\d+(?:[.,]\d+)?|\.\d+)"
    r"(?:\s?(?:percent|percentage|trillion|billion|million|bn|"
    r"ms|s|sec(?:ond)?s?|min(?:ute)?s?|h(?:ou)?rs?|kg|g|mg|km|cm|mm|"
    r"m²|m3|lit(?:er|re)s?|kw|mw|gw|°c|°f|k|m|l)(?![A-Za-z])|\s?%)?"
    r"(?![A-Za-z0-9])",
    flags=re.I,
)
MAGNITUDE_WORD_RE = re.compile(
    r"\b(?:hundreds?|thousands?|millions?|billions?|trillions?)\b",
    flags=re.I,
)
SPOKEN_QUANTITY_RE = re.compile(
    r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:[- ](?:one|two|three|"
    r"four|five|six|seven|eight|nine))?\s+(?:percent|percentage)\b",
    flags=re.I,
)


def canonical_quantity(value: Any) -> str:
    return re.sub(r"[\s,]+", "", str(value or "").strip().lower())


def quantified_tokens(text: Any) -> List[str]:
    value = str(text or "")
    found = [match.group(0).strip() for match in QUANTITY_RE.finditer(value)]
    found.extend(match.group(0).strip() for match in MAGNITUDE_WORD_RE.finditer(value))
    found.extend(match.group(0).strip() for match in SPOKEN_QUANTITY_RE.finditer(value))
    result: List[str] = []
    seen = set()
    for token in found:
        canonical = canonical_quantity(token)
        if canonical and canonical not in seen:
            seen.add(canonical)
            result.append(token)
    return result


def quantity_requires_source(value: Any, *, structural_context: bool = False) -> bool:
    """Return whether a quantitative literal needs source authorization.

    Notation with an empirical unit, scale, currency, percentage or calendar
    year is always source-locked. A bare unitless numeral is source-locked in
    narration and factual claims, but may be used in a structural visual
    context (cardinality, indexing, logic, mathematics or code). This separates
    semantic roles without relying on topic words or one input's vocabulary.
    """
    raw = str(value or "").strip()
    canonical = canonical_quantity(raw)
    if not canonical:
        return False
    if re.search(r"[$€£₹%]", raw):
        return True
    if MAGNITUDE_WORD_RE.fullmatch(raw):
        return True
    if re.search(r"[A-Za-z°²³]", raw):
        return True
    if "," in raw:
        return True
    if re.fullmatch(r"\d{4}", canonical):
        year = int(canonical)
        if 1600 <= year <= 2400:
            return True
    return not structural_context


__all__ = [
    "canonical_quantity",
    "quantified_tokens",
    "quantity_requires_source",
]
