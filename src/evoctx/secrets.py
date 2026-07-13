"""Write-time secret redaction — built-in patterns applied to everything
stored, regardless of which grant is active. Read-time redaction (redact.py)
is per-grant and optional; this is unconditional, because a secret that
never touches disk can't leak from a bypass, a raw `evoctx dump`, or a
stray file-access adversary. Best-effort: regex can't catch every secret
shape, reformatting, or a value the model paraphrases. Documented as such,
not sold as a guarantee.
"""
import re

# (name, pattern, replacement) — checked in order, each independent.
BUILTIN_SECRET_PATTERNS: list[dict] = [
    {"pattern": r"sk-[A-Za-z0-9]{20,}", "replace": "[REDACTED_API_KEY]"},
    {"pattern": r"sk_(live|test)_[A-Za-z0-9]{10,}", "replace": "[REDACTED_STRIPE_KEY]"},
    {"pattern": r"AKIA[0-9A-Z]{16}", "replace": "[REDACTED_AWS_KEY_ID]"},
    {"pattern": r"gh[pousr]_[A-Za-z0-9]{36,}", "replace": "[REDACTED_GITHUB_TOKEN]"},
    {"pattern": r"xox[baprs]-[A-Za-z0-9-]{10,}", "replace": "[REDACTED_SLACK_TOKEN]"},
    {"pattern": r"AIza[0-9A-Za-z_\-]{35}", "replace": "[REDACTED_GOOGLE_KEY]"},
    {
        "pattern": r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        "replace": "[REDACTED_PRIVATE_KEY]",
    },
    {
        "pattern": r"(?i)(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*['\"]?[^\s'\"]{6,}",
        "replace": "[REDACTED_CREDENTIAL]",
    },
]

_COMPILED = [
    (re.compile(p["pattern"], re.DOTALL), p["replace"]) for p in BUILTIN_SECRET_PATTERNS
]


def redact_secrets(text: str) -> tuple[str, int]:
    """Returns (possibly-redacted text, number of substitutions made)."""
    if not text:
        return text, 0
    hits = 0

    def sub(pattern, replacement, s):
        nonlocal hits
        s, n = pattern.subn(replacement, s)
        hits += n
        return s

    for pattern, replacement in _COMPILED:
        text = sub(pattern, replacement, text)
    return text, hits
