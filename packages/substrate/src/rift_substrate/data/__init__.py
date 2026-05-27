"""Data substrate — unified access to HL data on disk.

Three concerns:
  fields    — what's queryable, where it comes from, how to acquire it
  inventory — what's currently cached, what's missing
  access    — Data.load() — single entry point for loading

Plus resampling primitives (time/volume/dollar bars) under resample.py.

This module DOES NOT ingest data — that's `rift fetch` / `rift sync` /
`rift subscribe`, which write to the canonical paths under ~/.rift/data/.
"""

from rift_substrate.data.access import Data
from rift_substrate.data.fields import (
    FIELDS,
    FieldSpec,
    field_requires,
    fields_by_source,
)
from rift_substrate.data.inventory import (
    CoinInventory,
    InventoryReport,
    inventory,
)
from rift_substrate.data.resample import (
    to_dollar_bars,
    to_time_bars,
    to_volume_bars,
)

__all__ = [
    "CoinInventory",
    "Data",
    "FIELDS",
    "FieldSpec",
    "InventoryReport",
    "field_requires",
    "fields_by_source",
    "inventory",
    "to_dollar_bars",
    "to_time_bars",
    "to_volume_bars",
]
