"""Terminal and report output for the CLI.

Kept apart from ``cli/main.py`` so argument parsing and command wiring stay
readable; the aggregate table helpers live here together with the raw table,
JSON output and the HTML report split notice.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import click

from crc_checker.report.html import write_html
from crc_checker.report.serializer import reports_to_dict
from crc_checker.report.summary import summarize_by_frame, summarize_by_signal
from crc_checker.report.trends import check_sample_stats, format_rate


def _print_summary(report):
    """Print the sample-based summary and any run caveats."""
    passed, total = check_sample_stats(report)
    click.echo()
    click.echo("=" * 60)
    click.echo(f"SUMMARY: {report.total_frames} frames, "
               f"{passed} passed, {total - passed} failed "
               f"({format_rate(passed, total)} pass rate)")
    if report.frames_truncated:
        click.echo(click.style(
            f"WARNING: stopped at the frame cap ({report.frame_limit} frames); "
            "the rest of the trace was NOT verified.", fg="yellow", bold=True))
    if report.dlc_mismatch_skipped:
        click.echo(click.style(
            f"NOTE: {report.dlc_mismatch_skipped} frame(s) skipped by the "
            "DLC gate.", fg="yellow"))
    if total == 0:
        click.echo(click.style(
            "WARNING: no checks were performed (no matching protection "
            "signals).", fg="yellow", bold=True))


# pylint: disable=too-many-arguments,too-many-positional-arguments
def _output_report(report, output_fmt: str, output_file: str,
                   only_failures: bool, max_sections: int | None = None,
                   *, show_all: bool = False, group_by: str = "signal"):
    """Render verification report as table or HTML, then print the summary."""
    if output_fmt == "html":
        path = output_file or "crc_report.html"
        written: list[str] = []
        note = write_html(report, path, only_failures, max_sections=max_sections,
                          written_paths=written)
        if note:
            click.echo(f"Report restricted: {note}")
        click.echo(f"HTML: {path}")
        # Split parts (_p2.html…) are real outputs; a silent part list leaves
        # users with only the first page and no way to find the rest.
        for part in written[1:]:
            click.echo(f"HTML part: {part}")
        _print_summary(report)
    else:
        _print_table(report, only_failures, show_all=show_all,
                     group_by=group_by)


def _exit_after_reports(reports, fail_on_failure: bool,
                        min_pass_rate: float | None,
                        allow_empty: bool = False,
                        allow_truncated: bool = False) -> None:
    """Exit 1 when the run should fail a CI gate.

    ``--fail-on-failure`` catches any failed check; ``--min-pass-rate``
    applies to the aggregate across all reports. Gated runs with no checks or
    a truncated trace fail closed unless explicitly allowed.
    """
    passed = sum(check_sample_stats(report)[0] for report in reports)
    total = sum(check_sample_stats(report)[1] for report in reports)
    failed = total - passed
    gate_requested = fail_on_failure or min_pass_rate is not None
    rate = (passed / total * 100.0) if total else 100.0
    empty_failure = gate_requested and total == 0 and not allow_empty
    truncated_failure = (gate_requested and not allow_truncated
                         and any(report.frames_truncated for report in reports))
    if any((empty_failure, truncated_failure, (fail_on_failure and failed),
            (min_pass_rate is not None and rate < min_pass_rate))):
        raise click.exceptions.Exit(1)


def _print_json_reports(reports, output_path: str | None = None,
                        only_failures: bool = False) -> None:
    """Write a single JSON document for a run to standard output or a file."""
    document = {"schema_version": 1,
                **reports_to_dict(reports, only_failures=only_failures)}
    output = json.dumps(document, ensure_ascii=False, indent=2)
    if output_path:
        Path(output_path).write_text(output + "\n", encoding="utf-8")
    else:
        click.echo(output)


def _safe_report_stem(trace_path: str) -> str:
    """Filesystem-safe stem for the per-trace report suffix (keeps CJK)."""
    stem = Path(trace_path).stem if trace_path else "trace"
    cleaned = re.sub(r"[^\w.-]+", "_", stem).strip("._")
    return cleaned or "trace"


def _report_output_path(base_path: str, report, multiple: bool,
                        index: int | None = None) -> str:
    """Output path for one report.

    A single trace keeps the configured path untouched. With several traces the
    configured path would be overwritten by every iteration, so each report gets
    the trace's file stem (plus its 1-based index, which also disambiguates
    same-named traces from different directories) appended:
    ``report.html`` → ``report_1_trace_a.html``. Split parts derive from that
    unique name too.
    """
    if not multiple:
        return base_path
    path = Path(base_path or "crc_report.html")
    suffix = path.suffix or ".html"
    stem = _safe_report_stem(getattr(report, "trace_path", ""))
    if index is not None:
        stem = f"{index}_{stem}"
    return str(path.with_name(f"{path.stem}_{stem}{suffix}"))


# pylint: disable=too-many-locals
def _fit(text: str, width: int) -> str:
    """Truncate a table cell so one long DBC signal name cannot break the table."""
    text = str(text)
    return text if len(text) <= width else text[:width - 1] + "~"


def _terminal_text(text: str) -> str:
    """Make verifier prose safe for legacy Windows consoles (cp936/cp1252)."""
    return (str(text)
            .replace("·", "|")
            .replace("×", "x")
            .replace("—", "-")
            .replace("–", "-"))


def _print_table(report, only_failures: bool = False, *,
                 show_all: bool = False, group_by: str = "signal"):
    """Print the default signal/frame summary or the raw verdict list."""
    if show_all:
        _print_raw_table(report, only_failures)
    else:
        _print_grouped_table(report, only_failures, group_by)
    _print_summary(report)


def _print_raw_table(report, only_failures: bool = False):
    """Print every verdict as one row (``--show-all``)."""
    verdicts = report.failed if only_failures else report.verdicts

    if not verdicts:
        click.echo("No failed verdicts." if only_failures
                   else "No verification results.")
        return

    # Column widths
    ts_w, id_w, ch_w, sig_w, algo_w, exp_w, act_w = 12, 10, 5, 25, 16, 10, 10

    # Header
    header = (
        f"{'Timestamp':<{ts_w}} {'CAN ID':<{id_w}} {'CH':<{ch_w}} "
        f"{'Signal':<{sig_w}} {'Algorithm':<{algo_w}} {'Expected':<{exp_w}} "
        f"{'Actual':<{act_w}} Verdict"
    )
    click.echo(header)
    click.echo("-" * len(header))

    for v in verdicts:
        status = (
            click.style("PASS", fg="green")
            if v.is_pass
            else click.style("FAIL", fg="red", bold=True)
        )
        exp_str = f"0x{v.expected_value:0{max(1, (v.expected_value.bit_length() + 7) // 8) * 2}X}"
        act_str = f"0x{v.actual_value:0{max(1, (v.actual_value.bit_length() + 7) // 8) * 2}X}"
        # ``algorithm`` is only "crc"/"rolling_counter"; the real preset lives
        # in ``crc_algorithm`` and matters when a bus mixes algorithms.
        algorithm = v.crc_algorithm or v.algorithm

        line = (
            f"{v.timestamp:>{ts_w}.6f} "
            f"{f'0x{v.frame_id:03X}':<{id_w}} "
            f"{getattr(v, 'channel', 0):<{ch_w}} "
            f"{_fit(v.signal_name, sig_w):<{sig_w}} "
            f"{_fit(algorithm, algo_w):<{algo_w}} "
            f"{exp_str:<{exp_w}} "
            f"{act_str:<{act_w}} "
            f"{status}"
        )
        click.echo(line)

        if not v.is_pass and v.detail:
            click.echo(f"  {click.style(_terminal_text(v.detail), fg='yellow')}")


def _print_grouped_table(report, only_failures: bool, group_by: str):
    """Print sample-aware aggregates instead of one row per failing frame."""
    if group_by == "frame":
        _print_frame_summary(report, only_failures)
    else:
        _print_signal_summary(report, only_failures)


def _status_text(failed: int):
    """Coloured PASS/FAIL label for an aggregate row."""
    if failed:
        return click.style("FAIL", fg="red", bold=True)
    return click.style("PASS", fg="green")


def _first_failure_text(timestamp: float | None) -> str:
    """Compact first-failure timestamp (``-`` when the group passed)."""
    return "-" if timestamp is None else f"{timestamp:.6f}"


def _print_signal_summary(report, only_failures: bool):
    """One row per protected signal; counts are frames, not folded verdicts."""
    rows = summarize_by_signal(report)
    if only_failures:
        rows = [row for row in rows if row.failed]
    if not rows:
        click.echo("No failed verdicts." if only_failures
                   else "No verification results.")
        return

    id_w, ch_w, frame_w, sig_w, algo_w = 10, 5, 22, 24, 20
    header = (
        f"{'CAN ID':<{id_w}} {'CH':<{ch_w}} {'Frame':<{frame_w}} "
        f"{'Signal':<{sig_w}} {'Algorithm':<{algo_w}} "
        f"{'Checks':>8} {'Failed':>8} {'First failure':>14}  Verdict"
    )
    click.echo(header)
    click.echo("-" * len(header))
    for row in rows:
        click.echo(
            f"{f'0x{row.frame_id:03X}':<{id_w}} "
            f"{row.channel:<{ch_w}} "
            f"{_fit(row.frame_name, frame_w):<{frame_w}} "
            f"{_fit(row.signal_name, sig_w):<{sig_w}} "
            f"{_fit(row.algorithm, algo_w):<{algo_w}} "
            f"{row.checks:>8} {row.failed:>8} "
            f"{_first_failure_text(row.first_failure):>14}  "
            f"{_status_text(row.failed)}"
        )
        if row.failed and row.first_detail:
            click.echo(f"  {click.style(_terminal_text(row.first_detail), fg='yellow')}")


def _print_frame_summary(report, only_failures: bool):
    """One row per frame: how many of its signals failed, and how often."""
    rows = summarize_by_frame(report)
    if only_failures:
        rows = [row for row in rows if row.failed]
    if not rows:
        click.echo("No failed verdicts." if only_failures
                   else "No verification results.")
        return

    id_w, ch_w, frame_w = 10, 5, 22
    header = (
        f"{'CAN ID':<{id_w}} {'CH':<{ch_w}} {'Frame':<{frame_w}} "
        f"{'Signals':>8} {'Checks':>8} {'Failed':>8} "
        f"{'First failure':>14}  Verdict"
    )
    click.echo(header)
    click.echo("-" * len(header))
    for row in rows:
        signals = (f"{row.failed_signals}/{row.signals}"
                   if row.failed else str(row.signals))
        click.echo(
            f"{f'0x{row.frame_id:03X}':<{id_w}} "
            f"{row.channel:<{ch_w}} "
            f"{_fit(row.frame_name, frame_w):<{frame_w}} "
            f"{signals:>8} {row.checks:>8} {row.failed:>8} "
            f"{_first_failure_text(row.first_failure):>14}  "
            f"{_status_text(row.failed)}"
        )
        if row.failed and row.first_detail:
            click.echo(f"  {click.style(_terminal_text(row.first_detail), fg='yellow')}")
