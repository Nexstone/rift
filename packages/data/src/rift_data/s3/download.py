"""S3 hourly file download + on-disk cache.

Each hourly file is ~15-30 MB lz4-compressed (all coins bundled, ~300+ instruments).
We decompress to ~/.rift/raw/{YYYYMMDD}/{HH}.jsonl using atomic tmp+rename writes so
an interrupted run never leaves 0-byte placeholders. Downloads return only the cache
path — bytes are not kept in memory after writing.
"""

from __future__ import annotations

from pathlib import Path

S3_FILLS_BUCKET = "hl-mainnet-node-data"
S3_ARCHIVE_BUCKET = "hyperliquid-archive"
DEFAULT_START = "2023-09-01"

RAW_CACHE_DIR = Path.home() / ".rift" / "raw"


def check_aws_credentials() -> bool:
    """True if AWS credentials are present for S3 access."""
    try:
        import boto3
        session = boto3.Session()
        credentials = session.get_credentials()
        return credentials is not None and credentials.access_key is not None
    except Exception:
        return False


def get_s3_client():
    """Construct an S3 client pointing at the Hyperliquid bucket region.

    `max_pool_connections=50` lets ThreadPoolExecutor download a day's worth
    of hour files concurrently without queueing on boto3's default 10-conn
    pool. The L2 sync uses up to 24 parallel workers per day; downstream
    callers may push higher.
    """
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3",
        region_name="ap-northeast-1",
        config=Config(max_pool_connections=50),
    )


def download_hour(s3_client, date_str: str, hour: int) -> Path | None:
    """Download one hourly fills file from S3, decompress, cache to disk.

    Returns the cache path on success, None if the file is missing or fails.
    Atomic write (tmp + rename) so an interrupted run never leaves a 0-byte
    placeholder.
    """
    import lz4.frame

    cache_path = RAW_CACHE_DIR / date_str / f"{hour:02d}.jsonl"
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path

    # Stale 0-byte placeholder from prior interrupted run — remove so we retry.
    if cache_path.exists():
        try:
            cache_path.unlink()
        except OSError:
            pass

    key = f"node_fills_by_block/hourly/{date_str}/{hour}.lz4"
    try:
        resp = s3_client.get_object(
            Bucket=S3_FILLS_BUCKET,
            Key=key,
            RequestPayer="requester",
        )
        compressed = resp["Body"].read()
        raw = lz4.frame.decompress(compressed)
        del compressed

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        tmp.write_bytes(raw)
        del raw
        tmp.replace(cache_path)
        return cache_path
    except Exception:
        return None
