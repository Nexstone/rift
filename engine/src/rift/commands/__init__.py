"""Command modules — flat CLI surface (e.g. `rift sync`, `rift backtest`).

The split is by domain for readability; the user-facing command surface
remains flat. Each module imports the shared Typer `app` from `_shared`
and adds its commands via `@app.command(...)`.

Loaded for side effects in rift/cli.py.
"""
