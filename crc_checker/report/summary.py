"""Sample-aware CLI aggregation.

A broken CRC on a large trace produces one failing verdict per frame — tens of
thousands of identical table rows that hide the shape of the run. The CLI's
default view groups verdicts by protected signal (or by frame) and counts
samples, so the same information fits on one screen. ``--show-all`` remains the
raw per-verdict view.

Counting follows the report's sample-based contract
(:func:`crc_checker.report.trends.check_sample_stats`): an RC failure episode
folds many frames into one verdict, and ``repeat_count`` restores the frame
count. Summing these rows therefore agrees with the SUMMARY line.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SignalSummary:  # pylint: disable=too-many-instance-attributes
    """One protected signal (optionally one channel) aggregated over a trace."""

    frame_id: int
    frame_name: str
    channel: int
    signal_name: str
    algorithm: str
    checks: int = 0
    failed: int = 0
    first_failure: float | None = None
    first_detail: str = ""

    @property
    def passed(self) -> int:
        """Samples that passed."""
        return self.checks - self.failed

    @property
    def status(self) -> str:
        """``PASS`` or ``FAIL`` for the aggregated signal."""
        return "FAIL" if self.failed else "PASS"


@dataclass
class FrameSummary:  # pylint: disable=too-many-instance-attributes
    """One frame (optionally one channel) aggregated across its signals."""

    frame_id: int
    frame_name: str
    channel: int
    signals: int = 0
    failed_signals: int = 0
    checks: int = 0
    failed: int = 0
    first_failure: float | None = None
    first_detail: str = ""

    @property
    def status(self) -> str:
        """``PASS`` or ``FAIL`` for the aggregated frame."""
        return "FAIL" if self.failed else "PASS"


def _sample_count(verdict) -> int:
    """Frames represented by one verdict (folded RC episodes carry a count)."""
    return max(1, int(getattr(verdict, "repeat_count", 1) or 1))


def summarize_by_signal(report) -> list[SignalSummary]:
    """Aggregate verdicts by ``(frame, channel, signal, algorithm)``.

    ``channel`` is part of the key: per-channel RC state means the same signal
    on CH0 and CH1 is two independent results.
    """
    groups: dict[tuple[int, int, str, str], SignalSummary] = {}
    for verdict in report.verdicts:
        channel = getattr(verdict, "channel", 0)
        algorithm = verdict.crc_algorithm or verdict.algorithm
        key = (verdict.frame_id, channel, verdict.signal_name, algorithm)
        row = groups.get(key)
        if row is None:
            row = SignalSummary(
                frame_id=verdict.frame_id,
                frame_name=report.get_frame_name(verdict.frame_id),
                channel=channel,
                signal_name=verdict.signal_name,
                algorithm=algorithm,
            )
            groups[key] = row
        samples = _sample_count(verdict)
        row.checks += samples
        if not verdict.is_pass:
            row.failed += samples
            if row.first_failure is None or verdict.timestamp < row.first_failure:
                row.first_failure = verdict.timestamp
                row.first_detail = verdict.detail
    return sorted(groups.values(),
                  key=lambda row: (row.channel, row.frame_id,
                                   row.signal_name, row.algorithm))


def summarize_by_frame(report) -> list[FrameSummary]:
    """Aggregate verdicts by ``(frame, channel)``."""
    groups: dict[tuple[int, int], FrameSummary] = {}
    signals: dict[tuple[int, int], set[tuple[str, str]]] = {}
    failed_signals: dict[tuple[int, int], set[tuple[str, str]]] = {}
    for verdict in report.verdicts:
        channel = getattr(verdict, "channel", 0)
        key = (verdict.frame_id, channel)
        row = groups.get(key)
        if row is None:
            row = FrameSummary(
                frame_id=verdict.frame_id,
                frame_name=report.get_frame_name(verdict.frame_id),
                channel=channel,
            )
            groups[key] = row
            signals[key] = set()
            failed_signals[key] = set()
        algorithm = verdict.crc_algorithm or verdict.algorithm
        signal_key = (verdict.signal_name, algorithm)
        signals[key].add(signal_key)
        samples = _sample_count(verdict)
        row.checks += samples
        if not verdict.is_pass:
            failed_signals[key].add(signal_key)
            row.failed += samples
            if row.first_failure is None or verdict.timestamp < row.first_failure:
                row.first_failure = verdict.timestamp
                row.first_detail = verdict.detail
    for key, row in groups.items():
        row.signals = len(signals[key])
        row.failed_signals = len(failed_signals[key])
    return sorted(groups.values(), key=lambda row: (row.channel, row.frame_id))
