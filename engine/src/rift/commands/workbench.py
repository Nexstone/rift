"""Strategy workbench (config-as-data) commands — extracted from cli.py in Phase 6.

The user-facing command surface is unchanged. Each command is registered
on the shared Typer `app` in `rift.commands._shared`.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import typer

from rift.commands._shared import app, _emit, _hint, _sanitize_for_json


@app.command("workbench-create")
def workbench_create(
    strategy_name: str = typer.Argument(..., help="Name for the new strategy"),
    template: str = typer.Option("blank", "--template", help="Template: funding, vwap_reversion, trend_follow, blank"),
) -> None:
    """Create a new custom strategy from a template."""
    from rift.workbench import create_from_template, generate_and_save, list_configs

    if strategy_name in list_configs():
        _emit({"type": "error", "msg": f"Strategy '{strategy_name}' already exists. Pick a different name or delete it first."})
        sys.exit(1)

    try:
        config = create_from_template(template, strategy_name)
    except ValueError as e:
        _emit({"type": "error", "msg": str(e)})
        sys.exit(1)

    path = generate_and_save(config)

    _emit({
        "type": "result",
        "command": "workbench-create",
        "name": strategy_name,
        "template": template,
        "config": config.to_dict(),
        "generated_path": str(path),
    })
    _hint(f"Next: test with 'rift quick-test {strategy_name} --pair BTC'")


@app.command("workbench-list")
def workbench_list() -> None:
    """List all custom workbench strategies."""
    from rift.workbench import list_configs, StrategyConfig, CONFIGS_DIR

    names = list_configs()
    strategies = []
    for name in names:
        try:
            config = StrategyConfig.load(name)
            strategies.append({
                "name": config.name,
                "description": config.description,
                "timeframe": config.timeframe,
                "version": config.version,
                "entry_count": len(config.entry_conditions),
                "exit_count": len(config.exit_conditions),
                "filters": [k for k, v in config.filters.items() if v],
            })
        except Exception:
            strategies.append({"name": name, "description": "error loading", "version": 0})

    _emit({"type": "result", "command": "workbench-list", "strategies": strategies})


@app.command("workbench-show")
def workbench_show(
    strategy_name: str = typer.Argument(..., help="Strategy name"),
) -> None:
    """Show a workbench strategy config."""
    from rift.workbench import StrategyConfig

    try:
        config = StrategyConfig.load(strategy_name)
    except FileNotFoundError as e:
        _emit({"type": "error", "msg": str(e)})
        sys.exit(1)

    _emit({"type": "result", "command": "workbench-show", "config": config.to_dict()})


@app.command("workbench-update")
def workbench_update(
    strategy_name: str = typer.Argument(..., help="Strategy name"),
    config_json: str = typer.Argument(..., help="Full config JSON string"),
) -> None:
    """Update a workbench strategy config and regenerate code."""
    from rift.workbench import StrategyConfig, generate_and_save

    try:
        old_config = StrategyConfig.load(strategy_name)
    except FileNotFoundError as e:
        _emit({"type": "error", "msg": str(e)})
        sys.exit(1)

    try:
        new_data = json.loads(config_json)
    except json.JSONDecodeError as e:
        _emit({"type": "error", "msg": f"Invalid JSON: {e}"})
        sys.exit(1)

    new_data["name"] = strategy_name  # prevent rename via update
    new_config = StrategyConfig.from_dict(new_data)
    new_config.version = old_config.version + 1
    new_config.created_at = old_config.created_at

    path = generate_and_save(new_config)

    _emit({
        "type": "result",
        "command": "workbench-update",
        "name": strategy_name,
        "version": new_config.version,
        "generated_path": str(path),
        "config": new_config.to_dict(),
    })


@app.command("workbench-delete")
def workbench_delete(
    strategy_name: str = typer.Argument(..., help="Strategy name to delete"),
) -> None:
    """Delete a custom workbench strategy."""
    from rift.workbench import delete_config

    deleted = delete_config(strategy_name)
    _emit({
        "type": "result",
        "command": "workbench-delete",
        "name": strategy_name,
        "deleted": deleted,
    })


@app.command("workbench-generate")
def workbench_generate(
    strategy_name: str = typer.Argument(..., help="Strategy name"),
) -> None:
    """Regenerate Python code from a strategy config."""
    from rift.workbench import StrategyConfig, generate_and_save

    try:
        config = StrategyConfig.load(strategy_name)
    except FileNotFoundError as e:
        _emit({"type": "error", "msg": str(e)})
        sys.exit(1)

    path = generate_and_save(config)
    _emit({
        "type": "result",
        "command": "workbench-generate",
        "name": strategy_name,
        "generated_path": str(path),
    })


@app.command("workbench-templates")
def workbench_templates() -> None:
    """List available strategy templates."""
    from rift.workbench import TEMPLATES

    templates = []
    for name, config in TEMPLATES.items():
        templates.append({
            "name": name,
            "description": config.description,
            "timeframe": config.timeframe,
            "entry_count": len(config.entry_conditions),
            "exit_count": len(config.exit_conditions),
            "filters": [k for k, v in config.filters.items() if v],
        })

    _emit({"type": "result", "command": "workbench-templates", "templates": templates})


@app.command("workbench-components")
def workbench_components() -> None:
    """List available mixer components from validated strategies."""
    from rift.workbench import VALIDATED_COMPONENTS

    _emit({"type": "result", "command": "workbench-components", "components": VALIDATED_COMPONENTS})


