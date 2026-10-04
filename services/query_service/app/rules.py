
import re

_NUMBER = r"(?P<amount>\d{1,6}(?:[.,]\d{1,2})?)"
_CURRENCY_BEFORE = r"(?:\$|usd\s*|us\$)?"
_CURRENCY_AFTER = r"(?:\s*(?:dollars?|usd|bucks|\$|डॉलर|dólares))?"

# Words that mean "at most", in English, Spanish and Hindi.
_MAX_WORDS_BEFORE = (
    r"under|below|less than|cheaper than|max(?:imum)?|up ?to|upto|within|at most|"
    r"no more than|budget(?: of| is)?|"
    r"menos de|por menos de|por debajo de|hasta|m[aá]ximo"
)
_MAX_WORDS_AFTER = r"or less|max(?:imum)?|budget|se kam|से कम|तक"

_PATTERNS = [
    # "under $40", "below 40 dollars", "por menos de 40 dólares"
    re.compile(rf"\b(?:{_MAX_WORDS_BEFORE})\s*{_CURRENCY_BEFORE}{_NUMBER}{_CURRENCY_AFTER}", re.IGNORECASE),
    # "$40 or less", "40 dollars max", "$40 से कम"
    re.compile(rf"{_CURRENCY_BEFORE}{_NUMBER}{_CURRENCY_AFTER}\s*(?:{_MAX_WORDS_AFTER})", re.IGNORECASE),
    # a bare "$40" anywhere is treated as a budget
    re.compile(rf"\${_NUMBER}", re.IGNORECASE),
]

_DEVANAGARI = re.compile(r"[\u0900-\u097F]")
_SPACES = re.compile(r"\s+")


def extract_price(query: str) -> tuple[float | None, str]:
    """Return (max_price, query_without_the_price_phrase)."""
    for pattern in _PATTERNS:
        match = pattern.search(query)
        if match:
            amount = float(match.group("amount").replace(",", "."))
            remaining = (query[:match.start()] + " " + query[match.end():])
            remaining = _SPACES.sub(" ", remaining).strip(" ,.-")
            return (amount if amount > 0 else None), remaining
    return None, query.strip()


def guess_language(query: str) -> str:
    """Very rough: only used by the fallback. The LLM detects language properly."""
    if _DEVANAGARI.search(query):
        return "hi"
    return "und"  # undetermined


def rule_parse(query: str) -> dict:
    """Fallback parse without an LLM."""
    max_price, remaining = extract_price(query)
    return {
        "search_query": remaining or query.strip(),
        "max_price": max_price,
        "language": guess_language(query),
    }