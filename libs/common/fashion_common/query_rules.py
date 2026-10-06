"""
Deterministic parts of query parsing (no LLM).
Shared: the Query Service uses it, and the Gateway uses it as a fallback
when the Query Service is unavailable.

extract_price(): finds a maximum price in the query ("under $40", "below 40
dollars", "por menos de $40", "$40 से कम") and returns it with the price phrase
removed from the text. Code owns numbers; the LLM never does.

rule_parse(): the fallback used when the LLM is unavailable. It searches with
the user's own words minus the price phrase.

verify_llm_price(): when the regex finds no price, the LLM may PROPOSE one (any
language); code accepts it only if the number literally appears in the query
and the currency is known, then converts it to USD.
"""

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


# Fixed, approximate rates to USD (the catalogue is priced in USD).
# In production these would come from a rates service.
CURRENCY_TO_USD = {
    "USD": 1.0, "EUR": 1.08, "GBP": 1.27, "INR": 0.012, "CAD": 0.73,
    "AUD": 0.66, "JPY": 0.0067, "MXN": 0.055, "BRL": 0.18,
}

_ANY_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def _numbers_in(query: str) -> list[float]:
    """Every number written in the query. A comma followed by 1-2 digits is a
    decimal separator ("19,99" = 19.99); otherwise thousands ("1,500" = 1500)."""
    numbers = []
    for token in _ANY_NUMBER.findall(query):
        if "," in token:
            whole, fraction = token.split(",")
            token = f"{whole}.{fraction}" if len(fraction) <= 2 else whole + fraction
        numbers.append(float(token))
    return numbers


def verify_llm_price(query: str, amount: float | None, currency: str | None) -> float | None:
    """
    Accept a max price proposed by the LLM only if code can verify it.
    Returns the budget in USD, or None to reject (no price filter is applied).

    Accepted only when ALL hold:
      a. amount is a number > 0 and < 100000
      b. the amount literally appears in the query as digits (so the LLM can't invent one)
      c. the currency (USD if not given) is in CURRENCY_TO_USD
    Never raises.
    """
    try:
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            return None
        if not 0 < amount < 100_000:
            return None
        if not any(abs(n - amount) < 1e-9 for n in _numbers_in(query)):
            return None
        rate = CURRENCY_TO_USD.get((currency or "USD").strip().upper())
        if rate is None:
            return None
        return round(amount * rate, 2)
    except Exception:
        return None