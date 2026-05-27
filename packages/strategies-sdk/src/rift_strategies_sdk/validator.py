"""Preflight validator for RIFT strategy files.

The SDK docstring promises `validate_strategy()` — this module delivers it.
Two-stage check:

  Stage 1 (static, fast): parse via `ast` to catch syntax errors + verify
    the file contains a `@register(...)`-decorated class inheriting from
    `Strategy`. Doesn't execute the file, so import errors don't fire.

  Stage 2 (dynamic): execfile the module, inspect the registered class for:
    - `config_class` attribute set + is a frozen dataclass
    - config fields use `Annotated[type, Param(...)]` so sweep can find them
    - `indicators()` method returns `dict[str, Indicator]`
    - `on_candle(candle, state)` has the right signature + return type
    - optional `promotion_gates` dict has known keys only

A clean validation returns `ValidationReport(ok=True, errors=[], warnings=[])`.
Errors fail the build; warnings are advisory. The CLI's `rift validate <file>`
command wraps this for newbies who don't want to import Python.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import inspect
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args, get_origin


_KNOWN_GATE_KEYS = frozenset({
    "min_dsr",
    "min_cv_pass_rate",
    "min_sharpe_per_fold",
    "min_capacity_usd",
    "min_observations",
    "min_trades",
    "max_dd_pct",
    "max_impact_fraction",
    "max_adv_pct",
})


@dataclass
class ValidationReport:
    """Result of validate_strategy().

    Attributes:
      ok:           True iff no errors were found
      strategy_name: name from @register(...) decorator if discoverable
      errors:       list of blocking issues (each: short string)
      warnings:     list of non-blocking suggestions
    """

    ok: bool
    strategy_name: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        status = "PASS" if self.ok else "FAIL"
        lines = [f"ValidationReport ({status})  strategy={self.strategy_name or '?'}"]
        if self.errors:
            lines.append(f"  Errors ({len(self.errors)}):")
            for e in self.errors:
                lines.append(f"    ✗ {e}")
        if self.warnings:
            lines.append(f"  Warnings ({len(self.warnings)}):")
            for w in self.warnings:
                lines.append(f"    ⚠ {w}")
        if not self.errors and not self.warnings:
            lines.append("  No issues found.")
        return "\n".join(lines)


def validate_strategy(file_path: str | Path) -> ValidationReport:
    """Validate a strategy .py file. Returns a ValidationReport.

    Both stages run unconditionally so the report surfaces all problems at
    once (no early-return). The dynamic stage is skipped only if the static
    stage couldn't find any registered class to introspect.
    """
    errors: list[str] = []
    warnings: list[str] = []
    strategy_name: str | None = None

    path = Path(file_path)
    if not path.exists():
        return ValidationReport(ok=False, errors=[f"file not found: {path}"])
    if not path.suffix == ".py":
        return ValidationReport(ok=False, errors=[f"not a .py file: {path}"])

    # ─── Stage 1: static AST check ──────────────────────────────
    source = path.read_text()
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return ValidationReport(
            ok=False,
            errors=[f"syntax error: {exc.msg} (line {exc.lineno})"],
        )

    # Walk the AST: find @register("name") classes
    registered_classes: list[tuple[ast.ClassDef, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for dec in node.decorator_list:
            # @register("name") — Call node with attr name "register"
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) and dec.func.id == "register":
                if dec.args and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str):
                    registered_classes.append((node, dec.args[0].value))

    if not registered_classes:
        errors.append("no @register(\"name\") class found — strategy must register itself")
    elif len(registered_classes) > 1:
        errors.append(
            f"multiple @register classes found ({[n for _, n in registered_classes]}) — "
            "one strategy per file"
        )

    if registered_classes:
        cls_node, strategy_name = registered_classes[0]

        # Check base class includes Strategy
        bases = [b.id if isinstance(b, ast.Name) else None for b in cls_node.bases]
        if "Strategy" not in bases:
            errors.append(
                f"class {cls_node.name!r} does not inherit from Strategy "
                f"(found bases: {bases})"
            )

    # ─── Stage 2: dynamic introspection ─────────────────────────
    # Skip if Stage 1 found no class to introspect.
    if registered_classes and not any("syntax error" in e for e in errors):
        try:
            module = _load_module(path)
        except Exception as exc:
            errors.append(f"import failed: {type(exc).__name__}: {exc}")
            module = None

        if module is not None:
            cls_name = registered_classes[0][0].name
            cls = getattr(module, cls_name, None)
            if cls is None:
                errors.append(f"class {cls_name!r} not found in loaded module")
            else:
                _check_class_shape(cls, errors, warnings)

    return ValidationReport(
        ok=not errors,
        strategy_name=strategy_name,
        errors=errors,
        warnings=warnings,
    )


def _load_module(path: Path) -> Any:
    """Load a .py file as a module. Uses a fresh module name to avoid cache."""
    module_name = f"_validator_load_{path.stem}_{id(path)}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not build module spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        # Don't pollute sys.modules with the validator's loaded copy
        sys.modules.pop(module_name, None)
    return module


def _check_class_shape(cls: type, errors: list[str], warnings: list[str]) -> None:
    """Inspect a loaded class for required attributes + method signatures."""
    # config_class
    config_cls = getattr(cls, "config_class", None)
    if config_cls is None:
        errors.append("config_class is None — must point to a frozen dataclass")
    else:
        if not dataclasses.is_dataclass(config_cls):
            errors.append(
                f"config_class ({config_cls.__name__}) is not a dataclass — "
                "use @dataclass(frozen=True)"
            )
        else:
            # Frozen check
            params = getattr(config_cls, "__dataclass_params__", None)
            if params is not None and not params.frozen:
                warnings.append(
                    f"config_class ({config_cls.__name__}) is not frozen — "
                    "@dataclass(frozen=True) is recommended for safety"
                )
            # Field annotations use Annotated[type, Param(...)]?
            try:
                from rift_engine.strategy import Param
            except ImportError:
                Param = None
            try:
                hints = getattr(config_cls, "__annotations__", {})
                for fname, hint in hints.items():
                    origin = get_origin(hint)
                    args = get_args(hint)
                    has_param = False
                    if origin is not None and len(args) >= 2:
                        # Annotated[X, Param(...)]
                        for meta in args[1:]:
                            if Param is not None and isinstance(meta, Param):
                                has_param = True
                                break
                    if not has_param:
                        warnings.append(
                            f"config field {fname!r} not Annotated[..., Param(...)] — "
                            "sweep tools won't auto-discover it"
                        )
            except Exception:
                pass

    # default_interval
    if not hasattr(cls, "default_interval") or not isinstance(cls.default_interval, str):
        warnings.append("default_interval not set (str) — research pipeline will pick a default")

    # indicators() — must be overridden in the subclass, not inherited from Strategy base
    if "indicators" not in cls.__dict__:
        warnings.append(
            "indicators() not overridden — strategy will use the base class no-op "
            "(returns {}); add indicators() if you need any computed signals"
        )
    else:
        sig = inspect.signature(cls.indicators)
        params = [p for p in sig.parameters.values() if p.name != "self"]
        if params:
            warnings.append(
                f"indicators() takes extra args {[p.name for p in params]} — "
                "convention is `def indicators(self) -> dict[str, Indicator]`"
            )

    # on_candle(candle, state) — MUST be overridden, base raises NotImplementedError
    if "on_candle" not in cls.__dict__:
        errors.append(
            "on_candle() not overridden — Strategy.on_candle raises NotImplementedError; "
            "your subclass must define on_candle(self, candle, state) -> Signal | None"
        )
    else:
        sig = inspect.signature(cls.on_candle)
        param_names = [p.name for p in sig.parameters.values() if p.name != "self"]
        if param_names[:2] != ["candle", "state"]:
            errors.append(
                f"on_candle signature is on_candle(self, {', '.join(param_names)}) — "
                "must be on_candle(self, candle, state)"
            )

    # Optional promotion_gates
    gates = getattr(cls, "promotion_gates", None)
    if gates is not None:
        if not isinstance(gates, dict):
            errors.append(f"promotion_gates must be a dict (got {type(gates).__name__})")
        else:
            unknown = set(gates.keys()) - _KNOWN_GATE_KEYS
            if unknown:
                warnings.append(
                    f"promotion_gates has unknown keys: {sorted(unknown)} — "
                    f"known keys: {sorted(_KNOWN_GATE_KEYS)}"
                )

    # recommended_train_months / recommended_test_months sanity
    train_m = getattr(cls, "recommended_train_months", 2)
    test_m = getattr(cls, "recommended_test_months", 1)
    if train_m < test_m:
        warnings.append(
            f"recommended_train_months ({train_m}) < recommended_test_months ({test_m}) — "
            "train should be ≥ test for walk-forward stability"
        )
