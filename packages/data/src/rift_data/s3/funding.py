"""Funding rate sync from hyperliquid-archive bucket.

Source: s3://hyperliquid-archive/asset_ctxs/{YYYYMMDD}.csv.lz4
Lightweight — one CSV per day, all coins, ~few hundred KB.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

import polars as pl

from rift_data.s3.download import DEFAULT_START, S3_ARCHIVE_BUCKET, get_s3_client


def sync_funding(
    coin: str,
    start_date: str = DEFAULT_START,
    end_date: str = "",
    on_progress: Callable | None = None,
) -> pl.DataFrame:
    """Download funding rate history from S3 archive for a single coin."""
    import lz4.frame

    s3 = get_s3_client()
    coin_upper = coin.upper()

    if not end_date:
        end_date = datetime.utcnow().strftime("%Y-%m-%d")

    current = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")

    all_funding: list[dict] = []

    while current <= end:
        date_str = current.strftime("%Y%m%d")
        key = f"asset_ctxs/{date_str}.csv.lz4"

        try:
            data = s3.get_object(
                Bucket=S3_ARCHIVE_BUCKET,
                Key=key,
                RequestPayer="requester",
            )
            raw = lz4.frame.decompress(data["Body"].read()).decode("utf-8", errors="ignore")

            lines = raw.strip().split("\n")
            if len(lines) < 2:
                current += timedelta(days=1)
                continue

            header = lines[0].split(",")
            for line in lines[1:]:
                fields = line.split(",")
                if len(fields) < len(header):
                    continue
                row = dict(zip(header, fields))

                row_coin = row.get("coin", row.get("name", ""))
                if row_coin != coin_upper:
                    continue

                try:
                    ts = int(float(row.get("timestamp", row.get("time", 0))))
                    funding = float(row.get("funding", row.get("fundingRate", row.get("funding_rate", 0))))
                    all_funding.append({"timestamp": ts, "funding_rate": funding})
                except (ValueError, KeyError):
                    continue
        except Exception:
            pass

        current += timedelta(days=1)

    if not all_funding:
        return pl.DataFrame(schema={"timestamp": pl.Int64, "funding_rate": pl.Float64})

    return pl.DataFrame(all_funding).unique(subset=["timestamp"]).sort("timestamp")
