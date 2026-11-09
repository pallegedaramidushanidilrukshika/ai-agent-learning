"""User-attached reproduction evidence: recordings and logs instead of typed steps.

Accepted (text only, never executed):
  - HAR browser/network recordings (.har), including WebSocket frames
  - API or WebSocket message logs (.json)
  - application/error logs and stack traces (.log, .txt)
  - recorded test scripts, e.g. Playwright codegen output (.js, .ts, .cjs, .mjs, .py)

Secrets (auth headers, cookies, tokens, passwords, keys) are redacted before
anything is stored, and only the redacted copy is kept. Each file gets a short
summary that the AI uses to write a reproduction test.
"""

import hashlib
import json
import os
import re
import time
import uuid


MAX_FILES_PER_RUN = 5
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
MAX_NAME_LENGTH = 120
SUMMARY_CHARS = 3000
INDEX_NAME = "evidence_index.json"
ALLOWED_EXTENSIONS = {
    ".har": "har",
    ".json": "message_log",
    ".log": "log",
    ".txt": "log",
    ".js": "script",
    ".cjs": "script",
    ".mjs": "script",
    ".ts": "script",
    ".py": "script",
}
SENSITIVE_HEADERS = {
    "authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key",
    "api-key", "x-amz-security-token", "x-auth-token", "x-csrf-token",
}
REDACTED = "[REDACTED]"
_SECRET_PATTERNS = (
    (re.compile(r"(?im)^(\s*(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key|api-key|x-amz-security-token|x-auth-token)\s*[:=]\s*).+$"), r"\1" + REDACTED),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer " + REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\b(?:gsk|sk|pk|rk)_[A-Za-z0-9]{16,}\b"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), REDACTED),
    (re.compile(r"""(?i)("?(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret|aws_secret_access_key|session[_-]?token)"?\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,&;}]+)"""), r"\1" + '"' + REDACTED + '"'),
)


class EvidenceError(ValueError):
    pass


def redact_text(text):
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def safe_file_name(name):
    if not isinstance(name, str) or not name.strip():
        raise EvidenceError("Each attachment needs a file name.")
    base = os.path.basename(name.replace("\\", "/")).strip()
    if len(base) > MAX_NAME_LENGTH:
        raise EvidenceError(f"File name is longer than {MAX_NAME_LENGTH} characters: {base[:40]}...")
    extension = os.path.splitext(base)[1].lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise EvidenceError(
            f"{base}: unsupported file type. Attach .har, .json, .log, .txt, "
            ".js, .ts, .cjs, .mjs or .py files."
        )
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return cleaned, extension


def _redact_har(document):
    log = document.get("log") if isinstance(document, dict) else None
    entries = log.get("entries") if isinstance(log, dict) else None
    if not isinstance(entries, list):
        raise EvidenceError("The .har file does not contain log.entries.")
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for side in ("request", "response"):
            part = entry.get(side)
            if not isinstance(part, dict):
                continue
            headers = part.get("headers")
            if isinstance(headers, list):
                part["headers"] = [
                    dict(header, value=REDACTED)
                    if isinstance(header, dict) and str(header.get("name", "")).lower() in SENSITIVE_HEADERS
                    else header
                    for header in headers
                ]
            if "cookies" in part:
                part["cookies"] = []
            for container in ("postData", "content"):
                block = part.get(container)
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    block["text"] = redact_text(block["text"])
            if isinstance(part.get("url"), str):
                part["url"] = redact_text(part["url"])
        messages = entry.get("_webSocketMessages")
        if isinstance(messages, list):
            for message in messages:
                if isinstance(message, dict) and isinstance(message.get("data"), str):
                    message["data"] = redact_text(message["data"])
    return document


def _snippet(value, limit=200):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def _summarize_har(document):
    lines = []
    for entry in document["log"]["entries"][:40]:
        if not isinstance(entry, dict):
            continue
        request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
        response = entry.get("response") if isinstance(entry.get("response"), dict) else {}
        line = f"{request.get('method', '?')} {_snippet(request.get('url', ''), 160)} -> {response.get('status', '?')}"
        body = (request.get("postData") or {}).get("text") if isinstance(request.get("postData"), dict) else None
        if body:
            line += f" | request: {_snippet(body)}"
        content = (response.get("content") or {}).get("text") if isinstance(response.get("content"), dict) else None
        if content:
            line += f" | response: {_snippet(content)}"
        lines.append(line)
        for message in (entry.get("_webSocketMessages") or [])[:30]:
            if isinstance(message, dict):
                lines.append(f"  ws {message.get('type', '?')}: {_snippet(message.get('data', ''))}")
    return "\n".join(lines)


def _summarize_message_log(document):
    if isinstance(document, list):
        return "\n".join(f"{index + 1}. {_snippet(item)}" for index, item in enumerate(document[:40]))
    if isinstance(document, dict):
        return _snippet(document, SUMMARY_CHARS)
    return _snippet(document)


def _summarize_log(text):
    lines = text.splitlines()
    important = [
        line for line in lines
        if re.search(r"(?i)error|exception|fail|traceback|warn|denied|timeout|refused", line)
    ][:40]
    tail = lines[-40:]
    parts = []
    if important:
        parts.append("Error and warning lines:\n" + "\n".join(important))
    parts.append("Last lines:\n" + "\n".join(tail))
    return "\n\n".join(parts)


def prepare_attachment(name, content):
    """Validate, redact and summarize one attachment. Returns a record without storing it."""
    stored_name, extension = safe_file_name(name)
    if not isinstance(content, str):
        raise EvidenceError(f"{stored_name}: content must be text.")
    if "\x00" in content:
        raise EvidenceError(f"{stored_name}: binary files are not supported; attach a text export.")
    size = len(content.encode("utf-8"))
    if size == 0:
        raise EvidenceError(f"{stored_name}: the file is empty.")
    if size > MAX_FILE_BYTES:
        raise EvidenceError(f"{stored_name}: larger than {MAX_FILE_BYTES // 1024} KB.")
    kind = ALLOWED_EXTENSIONS[extension]
    if kind in ("har", "message_log"):
        try:
            document = json.loads(content)
        except ValueError:
            raise EvidenceError(f"{stored_name}: not valid JSON.") from None
        if kind == "har" or (isinstance(document, dict) and isinstance(document.get("log"), dict)):
            kind = "har"
            document = _redact_har(document)
            redacted = json.dumps(document, ensure_ascii=False, indent=1)
            summary = _summarize_har(document)
        else:
            redacted = redact_text(content)
            summary = _summarize_message_log(json.loads(redacted))
    else:
        redacted = redact_text(content)
        summary = _summarize_log(redacted) if kind == "log" else redacted
    return {
        "name": os.path.basename(name.replace("\\", "/"))[:MAX_NAME_LENGTH],
        "stored_name": stored_name,
        "kind": kind,
        "bytes": size,
        "redacted_content": redacted,
        "redacted": redacted != content,
        "summary": summary[:SUMMARY_CHARS],
    }


def load_index(run_directory):
    path = os.path.join(run_directory, INDEX_NAME)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) and isinstance(value.get("files"), list) else {"files": []}
    except (OSError, ValueError):
        return {"files": []}


def validate_attachments(attachments, existing=()):
    """Validate every attachment and the per-run limits. Returns prepared records."""
    if not isinstance(attachments, list) or not attachments:
        raise EvidenceError("Attach at least one file.")
    existing = list(existing)
    if len(existing) + len(attachments) > MAX_FILES_PER_RUN:
        raise EvidenceError(f"At most {MAX_FILES_PER_RUN} evidence files can be attached to one run.")
    prepared = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            raise EvidenceError("Each attachment must be an object with name and content.")
        prepared.append(prepare_attachment(attachment.get("name"), attachment.get("content")))
    total = sum(item.get("bytes", 0) for item in existing) + sum(item["bytes"] for item in prepared)
    if total > MAX_TOTAL_BYTES:
        raise EvidenceError(f"Evidence for one run must total at most {MAX_TOTAL_BYTES // 1024} KB.")
    return prepared


def store_attachments(run_directory, attachments):
    """Validate all attachments first, then store redacted copies and update the index."""
    index = load_index(run_directory)
    existing = index["files"]
    prepared = validate_attachments(attachments, existing)

    evidence_directory = os.path.join(run_directory, "evidence")
    os.makedirs(evidence_directory, exist_ok=True)
    records = []
    for offset, item in enumerate(prepared, start=len(existing) + 1):
        stored_as = f"{offset:02d}-{item['stored_name']}"
        path = os.path.join(evidence_directory, stored_as)
        with open(path, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(item["redacted_content"])
        records.append({
            "id": uuid.uuid4().hex,
            "name": item["name"],
            "stored_as": f"evidence/{stored_as}",
            "kind": item["kind"],
            "bytes": item["bytes"],
            "sha256": hashlib.sha256(item["redacted_content"].encode("utf-8")).hexdigest(),
            "redacted": item["redacted"],
            "summary": item["summary"],
            "added_at": time.time(),
        })
    index["files"] = existing + records
    from pathlib import Path

    from workflow_control import _atomic_json

    _atomic_json(Path(run_directory) / INDEX_NAME, index)
    return records


def public_summary(run_directory):
    """What the console may show: names and kinds, never file contents."""
    return [
        {key: item.get(key) for key in ("id", "name", "kind", "bytes", "redacted")}
        for item in load_index(run_directory)["files"]
        if isinstance(item, dict)
    ]
