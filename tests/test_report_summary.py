"""Aggregated CLI tables count samples, not folded verdicts."""

from crc_checker.report.summary import summarize_by_frame, summarize_by_signal
from crc_checker.report.trends import check_sample_stats
from crc_checker.verify.models import VerifyReport, VerifyVerdict


def _verdict(**overrides) -> VerifyVerdict:
    defaults = {
        "frame_id": 0x100,
        "timestamp": 0.1,
        "algorithm": "crc",
        "signal_name": "Checksum_Alpha",
        "expected_value": 0x12,
        "actual_value": 0x34,
        "is_pass": False,
        "crc_algorithm": "CRC8/SAE-J1850",
        "detail": "CRC mismatch",
    }
    defaults.update(overrides)
    return VerifyVerdict(**defaults)


def _report() -> VerifyReport:
    report = VerifyReport(verdicts=[
        _verdict(timestamp=0.1),
        _verdict(timestamp=0.2),
        _verdict(timestamp=0.4, is_pass=True),
        # A folded RC failure episode represents five frames.
        _verdict(timestamp=0.3, algorithm="rolling_counter",
                 signal_name="RollingCounter_Alpha", crc_algorithm="",
                 repeat_count=5, detail="STALE counter"),
    ])
    report.set_frame_name(0x100, "MsgAlpha")
    return report


def test_signal_summary_counts_folded_episodes_as_samples():
    """CRC rows are per frame; an RC episode contributes its repeat_count."""
    rows = summarize_by_signal(_report())
    assert [(row.signal_name, row.algorithm) for row in rows] == [
        ("Checksum_Alpha", "CRC8/SAE-J1850"),
        ("RollingCounter_Alpha", "rolling_counter"),
    ]
    crc, rc = rows
    assert (crc.checks, crc.failed, crc.passed) == (3, 2, 1)
    assert crc.first_failure == 0.1
    assert crc.first_detail == "CRC mismatch"
    assert (rc.checks, rc.failed, rc.passed) == (5, 5, 0)
    assert rc.status == "FAIL"
    assert crc.status == "FAIL"


def test_frame_summary_aggregates_its_signals():
    """One row per frame with the failed/total signal counts."""
    rows = summarize_by_frame(_report())
    assert len(rows) == 1
    row = rows[0]
    assert row.frame_name == "MsgAlpha"
    assert (row.signals, row.failed_signals) == (2, 2)
    assert (row.checks, row.failed) == (8, 7)
    assert row.first_failure == 0.1


def test_channel_is_part_of_the_grouping_key():
    """The same signal on two channels is two independent results."""
    report = VerifyReport(verdicts=[
        _verdict(channel=0),
        _verdict(channel=1, is_pass=True, crc_algorithm="CRC8/AUTOSAR"),
    ])
    report.set_frame_name(0x100, "MsgAlpha")
    rows = summarize_by_signal(report)
    assert [row.channel for row in rows] == [0, 1]
    assert [row.status for row in rows] == ["FAIL", "PASS"]


def test_summary_totals_match_without_rc_series():
    """Aggregate sample counts agree with the SUMMARY fallback (no series)."""
    report = _report()
    rows = summarize_by_signal(report)
    assert sum(row.checks for row in rows) == check_sample_stats(report)[1]
    assert sum(row.failed for row in rows) == (
        check_sample_stats(report)[1] - check_sample_stats(report)[0])
