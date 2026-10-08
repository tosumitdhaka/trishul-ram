from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from tram.connectors.file_sink_common import (
    file_state_key,
    render_filename,
    source_unit_key,
    validate_template_tokens,
)
from tram.connectors.local.sink import LocalSink
from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import DeliveryTier


def _opened_at() -> datetime:
    return datetime(2026, 4, 20, 12, 34, 56, tzinfo=UTC)


def _staging_meta(**overrides) -> dict:
    meta = {
        "pipeline_name": "mypipe",
        "run_id": "run-1",
        "source_filename": "input.ber",
        "source_path": "/in/input.ber",
        "serializer_type": "ndjson",
        "serializer_config": {"type": "ndjson"},
        "output_record_count": 1,
        "enable_safe_finalize": True,
    }
    meta.update(overrides)
    return meta


def test_render_filename_derives_source_stem_and_suffix() -> None:
    rendered = render_filename(
        "{source_stem}{source_suffix}",
        opened_at=_opened_at(),
        part_index=1,
        max_index=99999,
        meta={"source_filename": "input.csv"},
    )

    assert rendered == "input.csv"


def test_render_filename_handles_source_without_suffix() -> None:
    rendered = render_filename(
        "{source_stem}{source_suffix}",
        opened_at=_opened_at(),
        part_index=1,
        max_index=99999,
        meta={"source_filename": "README"},
    )

    assert rendered == "README"


def test_render_filename_falls_back_to_source_path_basename() -> None:
    rendered = render_filename(
        "{source_stem}{source_suffix}",
        opened_at=_opened_at(),
        part_index=1,
        max_index=99999,
        meta={"source_path": "/var/inbox/session/file.txt"},
    )

    assert rendered == "file.txt"


def test_render_filename_falls_back_to_data_when_source_missing() -> None:
    rendered = render_filename(
        "{source_stem}_{part}.csv",
        opened_at=_opened_at(),
        part_index=1,
        max_index=99999,
        meta={},
    )

    assert rendered == "data_00001.csv"


def test_render_filename_resolves_field_token() -> None:
    rendered = render_filename(
        "{field.nf_name}_{part}.ndjson",
        opened_at=_opened_at(),
        part_index=1,
        max_index=99999,
        meta={"field_values": {"nf_name": "SMSC"}},
    )

    assert rendered == "SMSC_00001.ndjson"


def test_render_filename_supports_epoch_ms_token() -> None:
    rendered = render_filename(
        "{epoch_ms}_{part}.ndjson",
        opened_at=_opened_at(),
        part_index=1,
        max_index=99999,
        meta={},
    )

    assert rendered == "1776688496000_00001.ndjson"


def test_render_filename_uses_unknown_for_missing_field_token() -> None:
    rendered = render_filename(
        "{field.nf_name}_{part}.ndjson",
        opened_at=_opened_at(),
        part_index=1,
        max_index=99999,
        meta={},
    )

    assert rendered == "unknown_00001.ndjson"


def test_file_state_key_excludes_rolling_tokens_and_includes_field_values() -> None:
    key = file_state_key(
        "{field.nf_name}_{source_stem}_{timestamp}_{part}.ndjson",
        meta={
            "source_filename": "input.csv",
            "field_values": {"nf_name": "MME"},
        },
    )

    assert key == (
        ("field.nf_name", "MME"),
        ("source_stem", "input"),
    )


def test_validate_template_tokens_rejects_unknown_tokens() -> None:
    issues = validate_template_tokens("{filename}_{part}.ndjson")

    assert issues == ["unknown template token 'filename'"]


# ── Publication manifests (V18-01 §7 file publication) ──────────────────────


def test_finalize_publishes_all_rolled_parts(tmp_path) -> None:
    """Rolling append with staging must publish EVERY part of the unit, not
    just the last one (the pre-manifest bookkeeping replaced the previous
    part's staged target on roll, orphaning it)."""
    sink = LocalSink({
        "path": str(tmp_path),
        "filename_template": "events_{part}.ndjson",
        "file_mode": "append",
        "max_records": 1,
    })
    meta = _staging_meta()
    sink.write(b'{"seq": 1}', meta)
    sink.write(b'{"seq": 2}', meta)
    sink.write(b'{"seq": 3}', meta)

    assert not (tmp_path / "events_00001.ndjson").exists()
    assert not (tmp_path / "events_00002.ndjson").exists()
    assert not (tmp_path / "events_00003.ndjson").exists()

    sink.finalize_source(meta, success=True)

    assert (tmp_path / "events_00001.ndjson").read_text() == '{"seq": 1}\n'
    assert (tmp_path / "events_00002.ndjson").read_text() == '{"seq": 2}\n'
    assert (tmp_path / "events_00003.ndjson").read_text() == '{"seq": 3}\n'
    assert not list(tmp_path.glob("*.tmp"))


def test_rename_failure_retains_input_and_staged_targets(tmp_path) -> None:
    """A failed rename raises but keeps BOTH the source input (which only the
    source's ack() may destroy) and the recoverable staged target; a retry
    resumes the publication."""
    sink = LocalSink({
        "path": str(tmp_path),
        "filename_template": "rows.ndjson",
        "file_mode": "single",
    })
    meta = _staging_meta()
    sink.write(b'{"seq": 1}', meta)

    temp = tmp_path / ".rows.ndjson.tram-run-1.tmp"
    assert temp.read_text() == '{"seq": 1}\n'

    # The source input file is never touched by the sink.
    input_file = tmp_path / "input.ber"
    input_file.write_bytes(b"source-payload")

    with patch.object(sink._backend, "replace", side_effect=OSError("rename failed")):
        with pytest.raises(SinkError, match="rename failed"):
            sink.finalize_source(meta, success=True)

    assert input_file.read_bytes() == b"source-payload"
    assert temp.read_text() == '{"seq": 1}\n'
    assert not (tmp_path / "rows.ndjson").exists()
    assert sink._writer.has_staged_targets(source_unit_key(meta))

    # Retry resumes the partial publication from the surviving staged temp.
    sink.finalize_source(meta, success=True)
    assert (tmp_path / "rows.ndjson").read_text() == '{"seq": 1}\n'
    assert not temp.exists()
    assert not sink._writer.has_staged_targets(source_unit_key(meta))


def test_partial_publication_resumes_without_truncating_confirmed_output(
    tmp_path,
) -> None:
    """Two rolled parts; the second rename fails after the first succeeded.
    The confirmed part is never re-renamed or truncated on the retry, and the
    second part is renamed from its surviving staged temp."""
    sink = LocalSink({
        "path": str(tmp_path),
        "filename_template": "events_{part}.ndjson",
        "file_mode": "append",
        "max_records": 1,
    })
    meta = _staging_meta()
    sink.write(b'{"seq": 1}', meta)
    sink.write(b'{"seq": 2}', meta)

    real_replace = sink._backend.replace

    def flaky_replace(handle, temp, final):
        if str(final).endswith("events_00002.ndjson"):
            raise OSError("rename failed for part 2")
        return real_replace(handle, temp, final)

    with patch.object(sink._backend, "replace", side_effect=flaky_replace):
        with pytest.raises(SinkError, match="rename failed for part 2"):
            sink.finalize_source(meta, success=True)

    # Part 1 is published (confirmed); part 2's temp survives.
    assert (tmp_path / "events_00001.ndjson").read_text() == '{"seq": 1}\n'
    part2_temp = tmp_path / ".events_00002.ndjson.tram-run-1.tmp"
    assert part2_temp.read_text() == '{"seq": 2}\n'
    assert not (tmp_path / "events_00002.ndjson").exists()

    # Retry resumes: part 1 is verified (temp already gone), part 2 renamed.
    sink.finalize_source(meta, success=True)
    assert (tmp_path / "events_00001.ndjson").read_text() == '{"seq": 1}\n'
    assert (tmp_path / "events_00002.ndjson").read_text() == '{"seq": 2}\n'
    assert not part2_temp.exists()
    assert not sink._writer.has_staged_targets(source_unit_key(meta))


def test_idempotent_republish_of_published_manifest(tmp_path) -> None:
    """Re-publishing an already-published unit is a no-op that never truncates
    the confirmed output."""
    sink = LocalSink({
        "path": str(tmp_path),
        "filename_template": "rows.ndjson",
        "file_mode": "single",
    })
    meta = _staging_meta()
    sink.write(b'{"seq": 1}', meta)
    sink.write(b'{"seq": 2}', meta)

    sink.finalize_source(meta, success=True)
    assert (tmp_path / "rows.ndjson").read_text() == '{"seq": 1}\n{"seq": 2}\n'

    sink.finalize_source(meta, success=True)
    assert (tmp_path / "rows.ndjson").read_text() == '{"seq": 1}\n{"seq": 2}\n'
    assert not (tmp_path / ".rows.ndjson.tram-run-1.tmp").exists()


def test_fsync_invoked_before_receipt(tmp_path) -> None:
    """fsynced_local tier: the temp file is fsynced before the atomic rename,
    and the parent directory is fsynced after — all before commit() confirms
    the receipt."""
    sink = LocalSink({
        "path": str(tmp_path),
        "filename_template": "rows.ndjson",
        "file_mode": "single",
    })
    meta = _staging_meta()
    sink.write(b'{"seq": 1}', meta)

    calls: list[str] = []
    real_replace = sink._backend.replace
    real_fsync = sink._backend.fsync
    real_fsync_dir = sink._backend.fsync_dir

    def spy_replace(handle, temp, final):
        calls.append(f"replace:{Path(final).name}")
        return real_replace(handle, temp, final)

    def spy_fsync(handle, path):
        calls.append(f"fsync:{Path(path).name}")
        return real_fsync(handle, path)

    def spy_fsync_dir(handle, path):
        calls.append(f"fsync_dir:{Path(path).name}")
        return real_fsync_dir(handle, path)

    sink._backend.replace = spy_replace
    sink._backend.fsync = spy_fsync
    sink._backend.fsync_dir = spy_fsync_dir

    receipt = sink.commit()

    assert receipt.confirmed is True
    assert receipt.tier == DeliveryTier.FSYNCED_LOCAL
    assert receipt.sink_key == "local"
    # file fsync → atomic rename → parent-directory fsync, then the receipt.
    assert calls.index("fsync:.rows.ndjson.tram-run-1.tmp") < calls.index("replace:rows.ndjson")
    assert calls.index("replace:rows.ndjson") < calls.index("fsync_dir:rows.ndjson")
    assert calls[-1] == "fsync_dir:rows.ndjson"
    assert (tmp_path / "rows.ndjson").read_text() == '{"seq": 1}\n'


def test_commit_latches_publication_failure(tmp_path) -> None:
    """A failed commit returns an unconfirmed receipt and latches the error;
    the staged temp survives for recovery and latched_error() is one-shot."""
    sink = LocalSink({
        "path": str(tmp_path),
        "filename_template": "rows.ndjson",
        "file_mode": "single",
    })
    meta = _staging_meta()
    sink.write(b'{"seq": 1}', meta)
    temp = tmp_path / ".rows.ndjson.tram-run-1.tmp"

    with patch.object(sink._backend, "replace", side_effect=OSError("boom")):
        receipt = sink.commit()

    assert receipt.confirmed is False
    assert receipt.tier == DeliveryTier.FSYNCED_LOCAL
    assert isinstance(sink.latched_error(), SinkError)
    assert sink.latched_error() is None  # surfaced once since the last check
    assert temp.exists()  # recoverable staged target retained
    assert not (tmp_path / "rows.ndjson").exists()

    # A retry succeeds and confirms the receipt.
    retry = sink.commit()
    assert retry.confirmed is True
    assert (tmp_path / "rows.ndjson").read_text() == '{"seq": 1}\n'
    assert not temp.exists()


def test_file_sinks_declare_fsynced_local_capability() -> None:
    """LocalSink and SFTPSink share the RollingWriter staged-publication
    helper and must declare the fsynced_local tier (V18-01 §6)."""
    from tram.connectors.sftp.sink import SFTPSink

    sink = LocalSink({"path": "/tmp/out", "filename_template": "out.ndjson"})
    assert sink.delivery_capability is not None
    assert sink.delivery_capability.tier == DeliveryTier.FSYNCED_LOCAL

    sftp = SFTPSink({
        "host": "example.com",
        "username": "user",
        "password": "pass",
        "remote_path": "/out",
    })
    assert sftp.delivery_capability is not None
    assert sftp.delivery_capability.tier == DeliveryTier.FSYNCED_LOCAL
    # Publication is not replay-safe (replay can mint a new file identity).
    assert sink.delivery_capability.replay_safe is False
