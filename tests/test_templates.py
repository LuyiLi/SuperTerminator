import pytest

from app.templates import (
    build_variables_schema,
    extract_variables,
    merge_template_values,
    render_template,
)


def test_extract_variables_deduplicates_in_order():
    command_template = "python train.py --lr {{lr}} --lr2 {{lr}} --cfg {{config_path}}"

    assert extract_variables(command_template) == ["lr", "config_path"]


def test_extract_variables_rejects_invalid_names():
    with pytest.raises(ValueError, match="Invalid template variable"):
        extract_variables("python train.py --bad {{bad-name}}")


def test_build_variables_schema_returns_default_required_fields():
    command_template = "python train.py --lr {{lr}} --cfg {{config_path}}"

    assert build_variables_schema(command_template) == [
        {
            "name": "lr",
            "label": "lr",
            "default": "",
            "description": "",
            "required": True,
        },
        {
            "name": "config_path",
            "label": "config_path",
            "default": "",
            "description": "",
            "required": True,
        },
    ]


def test_merge_template_values_uses_form_over_preset_over_defaults():
    variables_schema = [
        {"name": "lr", "default": "0.1"},
        {"name": "batch_size", "default": "32"},
        {"name": "epochs", "default": "10"},
    ]
    preset_values = {"lr": "0.01", "batch_size": "64"}
    form_values = {"lr": "0.001"}

    assert merge_template_values(variables_schema, preset_values, form_values) == {
        "lr": "0.001",
        "batch_size": "64",
        "epochs": "10",
    }


def test_render_template_raises_for_missing_required_variable():
    variables_schema = [{"name": "config", "required": True}]

    with pytest.raises(ValueError, match="Missing required variable: config"):
        render_template("python train.py --cfg {{config}}", variables_schema, {})


def test_render_template_replaces_values_and_form_override_wins():
    command_template = "python train.py --lr {{lr}} --cfg {{ config }}"
    variables_schema = build_variables_schema(command_template)
    values = merge_template_values(
        variables_schema,
        preset_values={"lr": "0.01", "config": "preset.yaml"},
        form_values={"lr": "0.001"},
    )

    assert render_template(command_template, variables_schema, values) == (
        "python train.py --lr 0.001 --cfg preset.yaml"
    )
