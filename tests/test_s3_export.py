import io
import json

import pytest

from benchmarking_service import s3_export
from benchmarking_service.models import MLflowTraceRecord, S3Config


ISS = "https://keycloak-keycloak.apps.ykt2.hcp.res.ibm.com/realms/kagenti"


class _FakeS3Client:
    """Captures put_object calls; can be told to reject ACLs like a bucket with ACLs disabled."""

    def __init__(self, reject_acl: bool = False):
        self.puts: list[dict] = []
        self.reject_acl = reject_acl

    def put_object(self, **kwargs):
        if self.reject_acl and "ACL" in kwargs:
            raise RuntimeError("AccessControlListNotSupported")
        self.puts.append(kwargs)


def _install_client(monkeypatch, fake):
    monkeypatch.setattr(s3_export, "_make_client", lambda cfg: fake)


def _records(n=2):
    return [
        MLflowTraceRecord(
            session_id=f"sess-{i}",
            agent_name="exgentic-a2a-tool-calling-gsm8k",
            benchmark_name="gsm8k",
            model="openai/x",
            num_parallel=1,
            status="OK",
            total_latency_s=1.5,
            evaluation_result=True,
        )
        for i in range(n)
    ]


def test_source_key_strips_scheme_and_sanitizes():
    assert (
        s3_export.source_key(ISS)
        == "keycloak-keycloak.apps.ykt2.hcp.res.ibm.com-realms-kagenti"
    )


def test_run_prefix_hierarchy_with_prefix():
    cfg = S3Config(bucket="b", prefix="bench/")
    prefix = s3_export.run_prefix(cfg, "alice", ISS, "gsm8k", "abc123")
    assert prefix == (
        "bench/alice/keycloak-keycloak.apps.ykt2.hcp.res.ibm.com-realms-kagenti/gsm8k/abc123"
    )


def test_run_prefix_no_prefix_and_sanitizes_username():
    cfg = S3Config(bucket="b")
    prefix = s3_export.run_prefix(cfg, "user@corp", ISS, "gsm8k", "r1")
    assert prefix.startswith("user-corp/")
    assert prefix.endswith("/gsm8k/r1")


def test_object_url_custom_endpoint_and_aws():
    minio = S3Config(bucket="bkt", endpoint_url="https://minio.local:9000/")
    assert s3_export._object_url(minio, "a/b.json") == "https://minio.local:9000/bkt/a/b.json"
    aws = S3Config(bucket="bkt", region="eu-west-1")
    assert s3_export._object_url(aws, "a/b.json") == "https://bkt.s3.eu-west-1.amazonaws.com/a/b.json"


def test_ndjson_bytes_roundtrip():
    rows = [{"a": 1}, {"a": 2}]
    lines = s3_export._ndjson_bytes(rows).decode().splitlines()
    assert [json.loads(x) for x in lines] == rows


def test_parquet_bytes_roundtrip():
    import pyarrow.parquet as pq

    rows = [{"session_id": "s1", "total_latency_s": 1.5}, {"session_id": "s2", "total_latency_s": 2.0}]
    table = pq.read_table(io.BytesIO(s3_export._parquet_bytes(rows)))
    assert table.column("session_id").to_pylist() == ["s1", "s2"]


async def test_export_run_uploads_all_three_formats(monkeypatch):
    fake = _FakeS3Client()
    _install_client(monkeypatch, fake)
    cfg = S3Config(bucket="bench-bkt", prefix="p")
    artifacts = await s3_export.export_run(
        cfg,
        preferred_username="alice",
        source_iss=ISS,
        benchmark="gsm8k",
        run_id="run-1",
        records=_records(2),
        run_summary={"run_id": "run-1", "status": "succeeded"},
    )
    names = {a.name for a in artifacts}
    assert names == {"run.json", "report.ndjson", "report.parquet"}
    # Every object went under the expected hierarchical prefix, public-read.
    for put in fake.puts:
        assert put["Bucket"] == "bench-bkt"
        assert put["Key"].startswith(
            "p/alice/keycloak-keycloak.apps.ykt2.hcp.res.ibm.com-realms-kagenti/gsm8k/run-1/"
        )
        assert put["ACL"] == "public-read"
    # NDJSON has one line per record.
    ndjson = next(p["Body"] for p in fake.puts if p["Key"].endswith("report.ndjson"))
    assert len(ndjson.decode().splitlines()) == 2
    # Artifact refs carry a usable url + size.
    parquet = next(a for a in artifacts if a.format == "parquet")
    assert parquet.size_bytes > 0
    assert parquet.url.endswith("/report.parquet")


async def test_export_run_skips_parquet_when_no_records(monkeypatch):
    fake = _FakeS3Client()
    _install_client(monkeypatch, fake)
    artifacts = await s3_export.export_run(
        S3Config(bucket="b"),
        preferred_username="alice",
        source_iss=ISS,
        benchmark="gsm8k",
        run_id="run-2",
        records=[],
        run_summary={"run_id": "run-2"},
    )
    names = {a.name for a in artifacts}
    assert names == {"run.json", "report.ndjson"}  # no parquet without a schema


async def test_export_run_retries_without_acl_when_rejected(monkeypatch):
    fake = _FakeS3Client(reject_acl=True)
    _install_client(monkeypatch, fake)
    artifacts = await s3_export.export_run(
        S3Config(bucket="b", public_read=True),
        preferred_username="alice",
        source_iss=ISS,
        benchmark="gsm8k",
        run_id="run-3",
        records=_records(1),
        run_summary={"run_id": "run-3"},
    )
    assert len(artifacts) == 3
    # The successful (retried) puts carry no ACL.
    assert all("ACL" not in p for p in fake.puts)


async def test_export_run_private_when_public_read_false(monkeypatch):
    fake = _FakeS3Client()
    _install_client(monkeypatch, fake)
    await s3_export.export_run(
        S3Config(bucket="b", public_read=False),
        preferred_username="alice",
        source_iss=ISS,
        benchmark="gsm8k",
        run_id="run-4",
        records=_records(1),
        run_summary={"run_id": "run-4"},
    )
    assert all("ACL" not in p for p in fake.puts)
