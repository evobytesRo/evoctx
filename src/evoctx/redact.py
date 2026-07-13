"""Response redaction — regex rewrites applied to everything leaving the store.

Rules come from the active grant's ``redactions`` list. Redaction is applied
to string *values* of responses (never keys), after scope filtering and
before the response leaves the policy engine.

Tokenization is stable per process: the same original text always becomes the
same placeholder within a session, and distinct originals hitting the same
rule get numbered placeholders ("[CLIENT]", "[CLIENT_2]", …) so the AI can
still tell entities apart without learning their real names.

The original→placeholder map is deliberately memory-only. It links real
values to placeholders across the whole session, which makes it more
sensitive than the data itself — it dies with the process and is never
written to disk or logs. (Client-side re-hydration of AI output is a
post-beta feature; v1 masking is one-way.)
"""
import re


class Redactor:
    def __init__(self, redactions: list[dict]):
        self._rules = [
            (re.compile(r["pattern"]), r["replace"]) for r in redactions or []
        ]
        self._map: dict[str, str] = {}      # original -> placeholder (memory only)
        self._counts: dict[str, int] = {}   # replace template -> distinct originals seen
        self.hits = 0                       # total substitutions this process

    @property
    def active(self) -> bool:
        return bool(self._rules)

    def _placeholder(self, original: str, template: str) -> str:
        if original in self._map:
            return self._map[original]
        n = self._counts.get(template, 0) + 1
        self._counts[template] = n
        if n == 1:
            placeholder = template
        elif template.endswith("]"):
            placeholder = f"{template[:-1]}_{n}]"
        else:
            placeholder = f"{template}_{n}"
        self._map[original] = placeholder
        return placeholder

    def redact_text(self, text: str) -> str:
        for pattern, template in self._rules:
            def sub(m: re.Match) -> str:
                self.hits += 1
                return self._placeholder(m.group(0), template)
            text = pattern.sub(sub, text)
        return text

    def redact(self, obj):
        """Walk dicts/lists and redact every string value. Keys untouched."""
        if not self._rules:
            return obj
        if isinstance(obj, str):
            return self.redact_text(obj)
        if isinstance(obj, list):
            return [self.redact(item) for item in obj]
        if isinstance(obj, dict):
            return {k: self.redact(v) for k, v in obj.items()}
        return obj
