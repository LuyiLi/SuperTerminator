"""Command template variable extraction, merging, and rendering."""

from __future__ import annotations

import re
from typing import Any

VAR_PATTERN = re.compile(r"{{\s*([^{}]+?)\s*}}")
VALID_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def extract_variables(command_template: str) -> list[str]:
    """Return unique template variable names in first-seen order."""
    variables: list[str] = []
    seen: set[str] = set()

    for match in VAR_PATTERN.finditer(command_template):
        name = match.group(1).strip()
        if not VALID_NAME_PATTERN.fullmatch(name):
            raise ValueError(f"Invalid template variable: {name}")
        if name not in seen:
            variables.append(name)
            seen.add(name)

    return variables


def build_variables_schema(command_template: str) -> list[dict[str, Any]]:
    """Build the default variables schema for a command template."""
    return [
        {
            "name": name,
            "label": name,
            "default": "",
            "description": "",
            "required": True,
        }
        for name in extract_variables(command_template)
    ]


def merge_template_values(
    variables_schema: list[dict[str, Any]],
    preset_values: dict[str, Any] | None,
    form_values: dict[str, Any] | None,
) -> dict[str, str]:
    """Merge values by priority: form values, then preset values, then schema defaults."""
    preset_values = preset_values or {}
    form_values = form_values or {}
    merged: dict[str, str] = {}

    for variable in variables_schema:
        name = str(variable["name"])
        value = variable.get("default", "")
        if name in preset_values:
            value = preset_values[name]
        if name in form_values:
            value = form_values[name]
        merged[name] = "" if value is None else str(value)

    return merged


def render_template(
    command_template: str,
    variables_schema: list[dict[str, Any]],
    values: dict[str, Any],
) -> str:
    """Render a command template with validated required variables."""
    for variable in variables_schema:
        name = str(variable["name"])
        if variable.get("required", False) and (name not in values or values[name] in (None, "")):
            raise ValueError(f"Missing required variable: {name}")

    def replace(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        if not VALID_NAME_PATTERN.fullmatch(name):
            raise ValueError(f"Invalid template variable: {name}")
        value = values.get(name, "")
        return "" if value is None else str(value)

    return VAR_PATTERN.sub(replace, command_template)
