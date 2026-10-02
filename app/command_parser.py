"""Parse shell-ish commands into editable parameters and history suggestions.

The parser is intentionally conservative: it understands the common command shapes used by
this app (exports, simple commands, GNU-style flags) and keeps opaque shell fragments as raw
text instead of trying to be a full shell parser.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import shlex
_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
_FLAG_RE = re.compile(r"^-{1,2}[A-Za-z0-9][A-Za-z0-9_.-]*")


@dataclass
class CommandParam:
    key: str
    value: str = ""
    kind: str = "option"  # env | option | flag | positional
    token_index: int | None = None
    value_index: int | None = None


@dataclass
class CommandLine:
    raw: str
    tokens: list[str]
    role: str
    params: list[CommandParam] = field(default_factory=list)


@dataclass
class ParsedCommand:
    lines: list[CommandLine]


def _logical_lines(command: str) -> list[str]:
    text = command.replace("\r\n", "\n").replace("\r", "\n")
    # Shell line continuation: join "\\\n" with one space.
    text = re.sub(r"\\\s*\n", " ", text)
    return [line.strip() for line in text.split("\n") if line.strip()]


def _split(line: str) -> list[str]:
    try:
        return shlex.split(line, posix=True)
    except ValueError:
        return line.split()


def _quote(token: str) -> str:
    if token == "":
        return "''"
    return shlex.quote(token)


def _line_role(tokens: list[str]) -> str:
    if not tokens:
        return "raw"
    if tokens[0] == "export" and len(tokens) >= 2 and _ASSIGN_RE.match(tokens[1]):
        return "env"
    if _ASSIGN_RE.match(tokens[0]):
        return "env"
    return f"cmd:{tokens[0]}"


def _parse_params(tokens: list[str], role: str) -> list[CommandParam]:
    params: list[CommandParam] = []
    if role == "env":
        offset = 1 if tokens and tokens[0] == "export" else 0
        for index in range(offset, len(tokens)):
            match = _ASSIGN_RE.match(tokens[index])
            if match:
                params.append(CommandParam(match.group(1), match.group(2), "env", index, index))
        return params

    i = 1 if role.startswith("cmd:") else 0
    positional_i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "--":
            i += 1
            continue
        if token.startswith("--") and "=" in token:
            key, value = token.split("=", 1)
            params.append(CommandParam(key, value, "option", i, i))
        elif _FLAG_RE.match(token):
            # If next token is not another flag, treat it as option value; otherwise boolean flag.
            if i + 1 < len(tokens) and not _FLAG_RE.match(tokens[i + 1]):
                params.append(CommandParam(token, tokens[i + 1], "option", i, i + 1))
                i += 1
            else:
                params.append(CommandParam(token, "", "flag", i, None))
        else:
            params.append(CommandParam(f"@{positional_i}", token, "positional", i, i))
            positional_i += 1
        i += 1
    return params


def parse_command(command: str) -> ParsedCommand:
    lines: list[CommandLine] = []
    for raw in _logical_lines(command):
        tokens = _split(raw)
        role = _line_role(tokens)
        lines.append(CommandLine(raw=raw, tokens=tokens, role=role, params=_parse_params(tokens, role)))
    return ParsedCommand(lines)


def render_command(parsed: ParsedCommand) -> str:
    rendered: list[str] = []
    for line in parsed.lines:
        if not line.tokens:
            continue
        rendered.append(" ".join(_quote(token) for token in line.tokens))
    return "\n".join(rendered)


def update_param(parsed: ParsedCommand, line_index: int, key: str, value: str) -> ParsedCommand:
    line = parsed.lines[line_index]
    for param in line.params:
        if param.key != key:
            continue
        if param.kind == "env" and param.token_index is not None:
            line.tokens[param.token_index] = f"{param.key}={value}"
        elif param.kind == "option":
            if param.value_index == param.token_index and param.token_index is not None:
                line.tokens[param.token_index] = f"{param.key}={value}"
            elif param.value_index is not None:
                line.tokens[param.value_index] = value
            elif param.token_index is not None:
                line.tokens.insert(param.token_index + 1, value)
        elif param.kind == "positional" and param.token_index is not None:
            line.tokens[param.token_index] = value
        # boolean flags intentionally ignore value edits
        reparsed = parse_command(render_command(parsed))
        parsed.lines = reparsed.lines
        return parsed
    return parsed


def delete_param(parsed: ParsedCommand, line_index: int, key: str) -> ParsedCommand:
    line = parsed.lines[line_index]
    for param in line.params:
        if param.key != key:
            continue
        indexes = [i for i in {param.token_index, param.value_index} if i is not None]
        for index in sorted(indexes, reverse=True):
            if 0 <= index < len(line.tokens):
                del line.tokens[index]
        reparsed = parse_command(render_command(parsed))
        parsed.lines = reparsed.lines
        return parsed
    return parsed


def add_param(parsed: ParsedCommand, line_index: int, key: str, value: str = "", kind: str = "option") -> ParsedCommand:
    line = parsed.lines[line_index]
    clean_key = key.strip()
    if not clean_key:
        return parsed
    if kind == "env":
        if line.tokens and line.tokens[0] == "export":
            line.tokens.append(f"{clean_key}={value}")
        else:
            parsed.lines.insert(line_index + 1, CommandLine(f"export {clean_key}={value}", ["export", f"{clean_key}={value}"], "env"))
    elif kind == "flag" or not value:
        line.tokens.append(clean_key)
    elif clean_key.startswith("--"):
        line.tokens.append(f"{clean_key}={value}")
    else:
        line.tokens.extend([clean_key, value])
    reparsed = parse_command(render_command(parsed))
    parsed.lines = reparsed.lines
    return parsed



def replace_param(
    parsed: ParsedCommand,
    line_index: int,
    old_key: str,
    new_key: str,
    value: str = "",
    kind: str | None = None,
) -> ParsedCommand:
    """Replace a parameter key/value in-place where possible."""
    line = parsed.lines[line_index]
    clean_key = new_key.strip()
    if not clean_key:
        return parsed
    for param in line.params:
        if param.key != old_key:
            continue
        target_kind = kind or param.kind
        if target_kind == "env":
            if param.token_index is not None:
                line.tokens[param.token_index] = f"{clean_key}={value}"
        elif target_kind == "flag":
            if param.token_index is not None:
                line.tokens[param.token_index] = clean_key
            if param.value_index is not None and param.value_index != param.token_index:
                del line.tokens[param.value_index]
        elif target_kind == "positional":
            if param.token_index is not None:
                line.tokens[param.token_index] = value or clean_key
        else:
            if param.value_index == param.token_index and param.token_index is not None:
                line.tokens[param.token_index] = f"{clean_key}={value}"
            elif param.value_index is not None and param.token_index is not None:
                line.tokens[param.token_index] = clean_key
                line.tokens[param.value_index] = value
            elif param.token_index is not None:
                line.tokens[param.token_index] = clean_key
                if value:
                    line.tokens.insert(param.token_index + 1, value)
        reparsed = parse_command(render_command(parsed))
        parsed.lines = reparsed.lines
        return parsed
    return parsed


def replace_line_tokens(parsed: ParsedCommand, line_index: int, command_word: str, rest: str = "") -> ParsedCommand:
    """Replace a line with a command word and shell-split rest tokens."""
    clean_command = command_word.strip()
    if not clean_command or line_index >= len(parsed.lines):
        return parsed
    rest_tokens = _split(rest) if rest.strip() else []
    parsed.lines[line_index].tokens = [clean_command, *rest_tokens]
    reparsed = parse_command(render_command(parsed))
    parsed.lines = reparsed.lines
    return parsed


def collect_line_history(commands: list[str]) -> dict[str, list[str]]:
    """Return editable line suggestions: first token and rest tokens, newest first."""
    history: dict[str, list[str]] = {"command": [], "rest": []}
    for command in commands:
        for line in parse_command(command).lines:
            if not line.tokens:
                continue
            first = line.tokens[0]
            rest = " ".join(line.tokens[1:])
            if first not in history["command"]:
                history["command"].append(first)
            if rest and rest not in history["rest"]:
                history["rest"].append(rest)
    return history


def collect_param_key_history(commands: list[str]) -> dict[str, list[str]]:
    """Return parameter key suggestions grouped by line role and kind."""
    history: dict[str, list[str]] = {}
    for command in commands:
        for line in parse_command(command).lines:
            for param in line.params:
                hist_key = f"{line.role}|{param.kind}"
                bucket = history.setdefault(hist_key, [])
                if param.key not in bucket:
                    bucket.append(param.key)
    return history

def collect_param_history(commands: list[str]) -> dict[str, list[str]]:
    """Return values keyed by role/key, preserving newest-first uniqueness from input order."""
    history: dict[str, list[str]] = {}
    for command in commands:
        parsed = parse_command(command)
        for line in parsed.lines:
            for param in line.params:
                if param.kind == "flag":
                    value = "<present>"
                else:
                    value = param.value
                hist_key = f"{line.role}|{param.kind}|{param.key}"
                bucket = history.setdefault(hist_key, [])
                if value not in bucket:
                    bucket.append(value)
    return history


def param_history_key(line: CommandLine, param: CommandParam) -> str:
    return f"{line.role}|{param.kind}|{param.key}"
