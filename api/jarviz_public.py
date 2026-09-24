"""Public task projection shared by HTTP and existing SessionChannel delivery."""
import json
import os
import re

_PRIVATE_KEY = re.compile(
    r"api.?key|token|secret|password|passwd|credential|authorization|"
    r"private.?key|traceback|stack.?trace|^(?:env|environment)$", re.I,
)
_TRACEBACK = re.compile(r'Traceback \(most recent call last\)|File "[^"\n]+", line \d+|stack trace:', re.I)
_ASSIGNMENT = re.compile(
    r"\b([\w]*(?:api_?key|token|secret|password|passwd|credential)[\w]*\s*[:=]\s*)"
    r"(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s,;]+)", re.I,
)
_CONTENT_FIELDS = frozenset({"title", "request", "assigned_agent", "task_type", "result", "error", "metadata_json"})


def public_value(value):
    """Redact public JSON content without changing its stored representation."""
    from api.helpers import _redact_text

    secrets = sorted({value for key, value in os.environ.items()
                      if _PRIVATE_KEY.search(key) and value}, key=len, reverse=True)

    def safe(value):
        if isinstance(value, dict):
            return {safe(key): "[redacted]" if _PRIVATE_KEY.search(key) else safe(child)
                    for key, child in value.items()}
        if isinstance(value, list):
            return [safe(child) for child in value]
        if not isinstance(value, str):
            return value
        if _TRACEBACK.search(value):
            return "[redacted traceback]"
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except ValueError:
                pass
            else:
                if isinstance(parsed, (dict, list)):
                    cleaned = safe(parsed)
                    return value if cleaned == parsed else json.dumps(cleaned, ensure_ascii=False)
        for secret in secrets:
            value = value.replace(secret, "[redacted]")
        value = _ASSIGNMENT.sub(lambda match: match[1] + "[redacted]", value)
        return _redact_text(value, _enabled=True)

    return safe(value)


def public_task(task):
    """Force content redaction while preserving task identity and timestamps."""
    content = public_value({key: value for key, value in task.items() if key in _CONTENT_FIELDS})
    return {**task, **content}
