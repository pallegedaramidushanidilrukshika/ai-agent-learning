"""Turn a Playwright codegen recording into a safe, replayable step list.

The uploaded script is never executed. Each line is either known codegen
boilerplate (imports, browser setup, closing calls), which is ignored, or one
browser step built only from an allowlist of locators, actions and assertions.
Anything else rejects the whole recording, with the line number.

The replay runner (browser_replay/replay.mjs) executes the resulting steps,
not the script, against the app the workflow started itself.
"""

import hashlib
import re
from urllib.parse import urlsplit


MAX_SCRIPT_BYTES = 64 * 1024
MAX_SCRIPT_LINES = 400
MAX_STEPS = 80
MAX_TEXT_LENGTH = 500
MAX_REGEX_LENGTH = 200
MAX_SOURCE_LENGTH = 300
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}

LOCATOR_METHODS = {
    "getByRole": {"name", "exact", "level", "checked"},
    "getByText": {"exact"},
    "getByLabel": {"exact"},
    "getByPlaceholder": {"exact"},
    "getByTestId": set(),
    "getByTitle": {"exact"},
    "getByAltText": {"exact"},
    "locator": {"hasText", "hasNotText"},
    "filter": {"hasText", "hasNotText"},
    "nth": set(),
    "first": set(),
    "last": set(),
}
ROOT_LOCATOR_METHODS = {
    name for name in LOCATOR_METHODS if name not in {"filter", "nth", "first", "last"}
}
# action -> (number of positional values, allowed options)
ACTIONS = {
    "click": (0, {"button"}),
    "dblclick": (0, set()),
    "fill": (1, set()),
    "press": (1, set()),
    "check": (0, set()),
    "uncheck": (0, set()),
    "selectOption": (1, set()),
    "hover": (0, set()),
}
# assertion -> kind of expected value (None: no value)
ASSERTIONS = {
    "toBeVisible": None,
    "toBeHidden": None,
    "toBeChecked": None,
    "toBeEnabled": None,
    "toBeDisabled": None,
    "toContainText": "text",
    "toHaveText": "text",
    "toHaveValue": "text",
    "toHaveCount": "count",
}
PYTHON_NAMES = {
    "get_by_role": "getByRole",
    "get_by_text": "getByText",
    "get_by_label": "getByLabel",
    "get_by_placeholder": "getByPlaceholder",
    "get_by_test_id": "getByTestId",
    "get_by_title": "getByTitle",
    "get_by_alt_text": "getByAltText",
    "select_option": "selectOption",
    "has_text": "hasText",
    "has_not_text": "hasNotText",
}
CLICK_BUTTONS = {"left", "right", "middle"}
KEY_PATTERN = re.compile(r"^[A-Za-z0-9+]{1,40}$")
ROLE_PATTERN = re.compile(r"^[a-z]{2,30}$")
REGEX_FLAGS = set("imsu")

JS_BOILERPLATE = [re.compile(pattern) for pattern in (
    r"^import\s+\{[\w\s,]+\}\s+from\s+['\"](?:@playwright/test|playwright)['\"];?$",
    r"^const\s+\{[\w\s,]+\}\s*=\s*require\(\s*['\"](?:@playwright/test|playwright)['\"]\s*\);?$",
    r"^test\(\s*(['\"])[^'\"]{0,200}\1\s*,\s*async\s*\(\s*\{\s*page\s*\}\s*\)\s*=>\s*\{$",
    r"^\(async\s*\(\)\s*=>\s*\{$",
    r"^\}\)(?:\(\))?;?$",
    r"^\};?$",
    r"^const\s+browser\s*=\s*await\s+(?:chromium|firefox|webkit)\.launch\(.*\);?$",
    r"^const\s+context\s*=\s*await\s+browser\.newContext\(.*\);?$",
    r"^const\s+page\s*=\s*await\s+context\.newPage\(\);?$",
    r"^await\s+(?:context|browser)\.close\(\);?$",
)]
# Codegen writes launch/newContext options over several lines in library mode:
#   const browser = await chromium.launch({
#     headless: false
#   });
JS_BLOCK_START = re.compile(
    r"^const\s+(?:browser|context)\s*=\s*await\s+"
    r"(?:(?:chromium|firefox|webkit)\.launch|browser\.newContext)\(\{$"
)
JS_BLOCK_LINE = re.compile(r"^[\w$]+\s*:\s*[\w'\".,\s{}:-]*,?$")
JS_BLOCK_END = re.compile(r"^\}\);?$")
PY_BOILERPLATE = [re.compile(pattern) for pattern in (
    r"^import\s+re$",
    r"^from\s+playwright\.sync_api\s+import\s+[\w\s,]+$",
    r"^def\s+\w+\(\s*(?:page:\s*Page|playwright:\s*Playwright)\s*\)\s*->\s*None:$",
    r"^browser\s*=\s*playwright\.(?:chromium|firefox|webkit)\.launch\(.*\)$",
    r"^context\s*=\s*browser\.new_context\(.*\)$",
    r"^page\s*=\s*context\.new_page\(\)$",
    r"^(?:context|browser)\.close\(\)$",
    r"^with\s+sync_playwright\(\)\s+as\s+playwright:$",
    r"^run\(playwright\)$",
)]


class RecordingError(ValueError):
    """The recording cannot be turned into safe steps."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


class _LineError(ValueError):
    pass


def detect_language(filename, text):
    suffix = (filename or "").lower().rsplit(".", 1)[-1] if "." in (filename or "") else ""
    if suffix == "py":
        return "python"
    if suffix in {"js", "ts", "mjs", "cjs"}:
        return "javascript"
    if re.search(r"^\s*from\s+playwright\.sync_api\s+import", text, re.M):
        return "python"
    return "javascript"


# ------------------------------------------------------------------
# Tokenizer (one statement per line, as codegen writes it)
# ------------------------------------------------------------------

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"', "`": "`", "0": "\0"}


def _read_string(line, index, raw=False):
    quote = line[index]
    index += 1
    chars = []
    while index < len(line):
        char = line[index]
        if char == quote:
            return "".join(chars), index + 1
        if char == "\\" and index + 1 < len(line):
            following = line[index + 1]
            if raw:
                chars.append(char + following)
                index += 2
                continue
            if following == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", line[index + 2:index + 6]):
                chars.append(chr(int(line[index + 2:index + 6], 16)))
                index += 6
                continue
            chars.append(_ESCAPES.get(following, following))
            index += 2
            continue
        if quote == "`" and char == "$" and line[index + 1:index + 2] == "{":
            raise _LineError("template expressions (${...}) are not allowed")
        chars.append(char)
        index += 1
    raise _LineError("unterminated string")


def _tokenize(line, language):
    tokens = []
    index = 0
    while index < len(line):
        char = line[index]
        if char.isspace():
            index += 1
            continue
        if language == "python" and char in "rR" and line[index + 1:index + 2] in ("'", '"'):
            value, index = _read_string(line, index + 1, raw=True)
            tokens.append(("str", value))
            continue
        if char in "'\"" or (char == "`" and language == "javascript"):
            value, index = _read_string(line, index)
            tokens.append(("str", value))
            continue
        if (
            char == "/"
            and language == "javascript"
            and (not tokens or tokens[-1] in (("punct", "("), ("punct", ","), ("punct", ":")))
        ):
            match = re.compile(r"/((?:\\.|\[(?:\\.|[^\]])*\]|[^/\\\[])+)/([a-z]*)").match(line, index)
            if not match:
                raise _LineError("invalid regular expression")
            tokens.append(("regex", (match.group(1), match.group(2))))
            index = match.end()
            continue
        if char.isdigit():
            match = re.compile(r"\d+").match(line, index)
            tokens.append(("num", int(match.group(0))))
            index = match.end()
            continue
        if char.isalpha() or char in "_$":
            match = re.compile(r"[A-Za-z_$][\w$]*").match(line, index)
            tokens.append(("ident", match.group(0)))
            index = match.end()
            continue
        if char in "().,{}:;=":
            tokens.append(("punct", char))
            index += 1
            continue
        raise _LineError(f"unexpected character {char!r}")
    return tokens


# ------------------------------------------------------------------
# Parser
# ------------------------------------------------------------------

class _Parser:
    def __init__(self, tokens, language):
        self.tokens = tokens
        self.language = language
        self.position = 0

    def peek(self, offset=0):
        index = self.position + offset
        return self.tokens[index] if index < len(self.tokens) else (None, None)

    def take(self):
        token = self.peek()
        self.position += 1
        return token

    def expect_punct(self, char):
        if self.take() != ("punct", char):
            raise _LineError(f"expected {char!r}")

    def expect_ident(self):
        kind, value = self.take()
        if kind != "ident":
            raise _LineError("expected a name")
        return PYTHON_NAMES.get(value, value) if self.language == "python" else value

    def at_end(self):
        while self.peek() == ("punct", ";"):
            self.position += 1
        return self.position >= len(self.tokens)

    def literal(self):
        kind, value = self.take()
        if kind == "str":
            return _checked_text(value)
        if kind == "num":
            return value
        if kind == "regex":
            return _checked_regex(*value)
        if kind == "ident":
            if value in ("true", "True"):
                return True
            if value in ("false", "False"):
                return False
            if value == "re" and self.language == "python":
                return self.python_regex()
        raise _LineError("only plain text, numbers, true/false or a regular expression are allowed as values")

    def python_regex(self):
        self.expect_punct(".")
        if self.expect_ident() != "compile":
            raise _LineError("only re.compile(...) is allowed")
        self.expect_punct("(")
        kind, pattern = self.take()
        if kind != "str":
            raise _LineError("re.compile needs a text pattern")
        flags = ""
        if self.peek() == ("punct", ","):
            self.take()
            if (self.take(), self.take(), self.take()) != (("ident", "re"), ("punct", "."), ("ident", "IGNORECASE")):
                raise _LineError("only the re.IGNORECASE flag is allowed")
            flags = "i"
        self.expect_punct(")")
        return _checked_regex(pattern, flags)

    def arguments(self):
        """Return (positional values, options) for a call; '(' already consumed."""
        positional, options = [], {}
        if self.peek() == ("punct", ")"):
            self.take()
            return positional, options
        while True:
            if self.language == "javascript" and self.peek() == ("punct", "{"):
                self.take()
                while self.peek() != ("punct", "}"):
                    kind, key = self.take()
                    if kind not in ("ident", "str"):
                        raise _LineError("invalid option name")
                    self.expect_punct(":")
                    options[key] = self.literal()
                    if self.peek() == ("punct", ","):
                        self.take()
                self.take()
            elif self.language == "python" and self.peek()[0] == "ident" and self.peek(1) == ("punct", "="):
                key = self.expect_ident()
                self.take()
                options[key] = self.literal()
            else:
                if options:
                    raise _LineError("positional values must come before options")
                positional.append(self.literal())
            kind, value = self.take()
            if (kind, value) == ("punct", ")"):
                return positional, options
            if (kind, value) != ("punct", ","):
                raise _LineError("expected ',' or ')'")
            if self.peek() == ("punct", ")"):
                self.take()
                return positional, options

    def call(self, name):
        self.expect_punct("(")
        return self.arguments()

    def locator_part(self, name):
        if name not in LOCATOR_METHODS:
            raise _LineError(f"{name!r} is not an allowed locator")
        if name in ("first", "last") and self.language == "python" and self.peek() != ("punct", "("):
            return {"method": name, "args": []}
        positional, options = self.call(name)
        unknown = set(options) - LOCATOR_METHODS[name]
        if unknown:
            raise _LineError(f"option {sorted(unknown)[0]!r} is not allowed for {name}")
        if name in ("first", "last"):
            if positional:
                raise _LineError(f"{name} takes no values")
        elif name == "nth":
            if len(positional) != 1 or type(positional[0]) is not int:
                raise _LineError("nth needs one whole number")
        elif name == "filter":
            if positional or not options:
                raise _LineError("filter needs hasText or hasNotText")
        else:
            if len(positional) != 1 or not isinstance(positional[0], (str, dict)):
                raise _LineError(f"{name} needs one text value")
            if name in ("getByRole", "locator", "getByTestId") and not isinstance(positional[0], str):
                raise _LineError(f"{name} needs plain text, not a regular expression")
            if name == "getByRole" and not ROLE_PATTERN.fullmatch(positional[0]):
                raise _LineError("invalid ARIA role")
        part = {"method": name, "args": positional}
        if options:
            part["options"] = options
        return part

    def locator_chain(self):
        """Parse `page.<locator>(...)...`; stops before an action name."""
        if self.take() != ("ident", "page"):
            raise _LineError("steps must start from page")
        chain = []
        while self.peek() == ("punct", "."):
            name_token = self.peek(1)
            if name_token[0] != "ident":
                raise _LineError("expected a method name")
            name = PYTHON_NAMES.get(name_token[1], name_token[1]) if self.language == "python" else name_token[1]
            if name not in LOCATOR_METHODS:
                break
            self.position += 2
            if not chain and name not in ROOT_LOCATOR_METHODS:
                raise _LineError(f"{name} cannot start a locator")
            chain.append(self.locator_part(name))
        return chain

    def statement(self):
        if self.peek() == ("ident", "await"):
            self.take()
        if self.peek() == ("ident", "expect"):
            return self.assertion()
        if self.peek() == ("ident", "page") and self.peek(1) == ("punct", ".") and self.peek(2) == ("ident", "goto"):
            self.position += 3
            positional, options = self.call("goto")
            if len(positional) != 1 or not isinstance(positional[0], str) or options:
                raise _LineError("goto needs exactly one URL")
            return {"action": "goto", "url": positional[0]}
        chain = self.locator_chain()
        if not chain:
            raise _LineError("expected a locator such as getByRole(...)")
        self.expect_punct(".")
        action = self.expect_ident()
        if action not in ACTIONS:
            raise _LineError(f"{action!r} is not an allowed action")
        positional, options = self.call(action)
        count, allowed_options = ACTIONS[action]
        if set(options) - allowed_options:
            raise _LineError(f"options are not allowed for {action}")
        if len(positional) != count or not all(isinstance(value, str) for value in positional):
            raise _LineError(f"{action} needs {count} text value(s)")
        if action == "press" and not KEY_PATTERN.fullmatch(positional[0]):
            raise _LineError("invalid key name")
        if "button" in options and options["button"] not in CLICK_BUTTONS:
            raise _LineError("invalid mouse button")
        step = {"action": action, "locator": chain}
        if positional:
            step["value"] = positional[0]
        if options:
            step["options"] = options
        return step

    def assertion(self):
        self.take()
        self.expect_punct("(")
        chain = self.locator_chain()
        if not chain:
            raise _LineError("expect(...) needs an element locator")
        self.expect_punct(")")
        self.expect_punct(".")
        name = self.expect_ident()
        negated = False
        if self.language == "javascript" and name == "not":
            negated = True
            self.expect_punct(".")
            name = self.expect_ident()
        elif self.language == "python":
            if name.startswith("not_to_"):
                negated, name = True, name[len("not_"):]
            if name.startswith("to_"):
                parts = name.split("_")
                name = parts[0] + "".join(part.capitalize() for part in parts[1:])
        if name not in ASSERTIONS:
            raise _LineError(f"{name!r} is not an allowed assertion")
        positional, options = self.call(name)
        if options:
            raise _LineError("assertion options are not allowed")
        kind = ASSERTIONS[name]
        step = {"action": "expect", "locator": chain, "assertion": name, "negated": negated}
        if kind is None:
            if positional:
                raise _LineError(f"{name} takes no value")
        elif kind == "count":
            if len(positional) != 1 or type(positional[0]) is not int:
                raise _LineError(f"{name} needs one whole number")
            step["expected"] = positional[0]
        else:
            if len(positional) != 1 or not isinstance(positional[0], (str, dict)):
                raise _LineError(f"{name} needs one text value or regular expression")
            step["expected"] = positional[0]
        return step


def _checked_text(value):
    if len(value) > MAX_TEXT_LENGTH:
        raise _LineError(f"text values are limited to {MAX_TEXT_LENGTH} characters")
    return value


def _checked_regex(pattern, flags):
    if len(pattern) > MAX_REGEX_LENGTH:
        raise _LineError(f"regular expressions are limited to {MAX_REGEX_LENGTH} characters")
    if set(flags) - REGEX_FLAGS:
        raise _LineError("unsupported regular expression flag")
    try:
        re.compile(pattern)
    except re.error:
        raise _LineError("invalid regular expression") from None
    return {"regex": pattern, "flags": flags}


def _local_origin(url):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise _LineError("goto must open an http(s) URL")
    host = parts.hostname.lower()
    if host not in LOCAL_HOSTS:
        raise _LineError("goto may only open the local app (localhost or 127.0.0.1)")
    if parts.username or parts.password:
        raise _LineError("URLs with credentials are not allowed")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    if parts.fragment:
        path += "#" + parts.fragment
    return f"{parts.scheme}://{parts.netloc.lower()}", path


def parse_recording(text, filename=""):
    """Return the safe step list for a codegen recording, or raise RecordingError."""
    if not isinstance(text, str):
        raise RecordingError(["The recording must be text."])
    if len(text.encode("utf-8")) > MAX_SCRIPT_BYTES:
        raise RecordingError([f"The recording is larger than {MAX_SCRIPT_BYTES // 1024} KB."])
    lines = text.splitlines()
    if len(lines) > MAX_SCRIPT_LINES:
        raise RecordingError([f"The recording has more than {MAX_SCRIPT_LINES} lines."])
    language = detect_language(filename, text)
    boilerplate = PY_BOILERPLATE if language == "python" else JS_BOILERPLATE
    comment = "#" if language == "python" else "//"

    steps, errors = [], []
    origin = None
    in_setup_block = False
    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if in_setup_block:
            if JS_BLOCK_END.fullmatch(line):
                in_setup_block = False
                continue
            if JS_BLOCK_LINE.fullmatch(line):
                continue
            errors.append(f"Line {number}: unexpected browser setup option -> {line[:120]}")
            in_setup_block = False
            continue
        if language == "javascript" and JS_BLOCK_START.fullmatch(line):
            in_setup_block = True
            continue
        if not line or line.startswith(comment) or any(p.fullmatch(line) for p in boilerplate):
            continue
        try:
            parser = _Parser(_tokenize(line, language), language)
            step = parser.statement()
            if not parser.at_end():
                raise _LineError("unexpected text after the step")
            if step["action"] == "goto":
                step_origin, step["path"] = _local_origin(step.pop("url"))
                if origin is None:
                    origin = step_origin
                elif step_origin != origin:
                    raise _LineError("all pages must be on the same local app")
            elif not steps and not errors:
                raise _LineError("the recording must start by opening a page (goto)")
        except _LineError as error:
            errors.append(f"Line {number}: {error} -> {line[:120]}")
            continue
        step["line"] = number
        step["source"] = line[:MAX_SOURCE_LENGTH]
        steps.append(step)

    if errors:
        raise RecordingError(errors[:10])
    if not steps:
        raise RecordingError(["The recording contains no browser steps."])
    if len(steps) > MAX_STEPS:
        raise RecordingError([f"The recording has more than {MAX_STEPS} steps."])
    if not any(step["action"] == "expect" for step in steps):
        raise RecordingError([
            "The recording has no assertion. Use the codegen toolbar "
            "(Assert visibility / text / value) to record what the correct app should show."
        ])
    return {
        "language": language,
        "recorded_origin": origin,
        "steps": steps,
        "assertions": sum(step["action"] == "expect" for step in steps),
        "script_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
