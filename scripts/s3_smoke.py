"""Smoke-test S3 export: write an object with the configured key, then read it back keyless.

Uses the same S3Config + object-URL logic the export path uses, so a PASS means a real run's
artifacts will both upload (write key works) and be publicly downloadable (bucket policy works).

    uv run python scripts/s3_smoke.py [--instance instances/<file>.json] [--keep]

The instance's S3 write key is PutObject-only, so the test object cannot be deleted from here;
it lands under a `_smoke/` prefix and is harmless. Exit code is nonzero on any failure.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

import httpx

from benchmarking_service.models import S3Config
from benchmarking_service import s3_export

DEFAULT_INSTANCE = "instances/keycloak.localtest.me_8080.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--instance", default=DEFAULT_INSTANCE, help="instance config JSON to read s3 from")
    args = ap.parse_args()

    raw = json.load(open(args.instance))
    cfg = S3Config(**raw.get("s3", {}))
    if not cfg.bucket:
        print(f"FAIL: {args.instance} has no s3.bucket configured")
        return 2

    token = uuid.uuid4().hex
    body = f"rossoctl-benchmarking s3 smoke {token}\n".encode()
    key = f"_smoke/{int(time.time())}-{token}.txt"
    url = s3_export._object_url(cfg, key)

    print(f"bucket={cfg.bucket} region={cfg.region} public_read={cfg.public_read}")
    print(f"PUT   {key}")
    client = s3_export._make_client(cfg)
    try:
        s3_export._put(client, cfg.bucket, key, body, "text/plain", cfg.public_read)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: write with the configured key failed: {exc!r}")
        return 1
    print("      write OK")

    print(f"GET   {url}  (no credentials)")
    try:
        resp = httpx.get(url, timeout=15.0)
    except httpx.HTTPError as exc:
        print(f"FAIL: keyless GET raised: {exc!r}")
        return 1
    if resp.status_code != 200:
        print(f"FAIL: keyless GET returned {resp.status_code} — bucket policy / public-access-block?")
        print(f"      body: {resp.text[:200]!r}")
        return 1
    if resp.content != body:
        print(f"FAIL: downloaded bytes differ from uploaded: {resp.content[:80]!r}")
        return 1

    print("      keyless read OK — bytes match")
    print(f"PASS  object is public at {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
