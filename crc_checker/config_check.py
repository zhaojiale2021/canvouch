"""Config-file diagnostics used by ``canvouch validate-config``.

The goal is to surface configuration mistakes *before* a trace run without
parsing any trace file. Everything here is read-only: it resolves the paths the
loader resolved, checks they exist, and validates the cheap, self-contained
pieces (workbook sheets, named mappings, pattern files, output directory).

The report is a flat list of :class:`ConfigIssue` rows so the CLI (and future
callers) can print per-section diagnostics and decide the exit code.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from crc_checker.config import RunConfig, load_config
from crc_checker.dataid.mappings import load_dataid_mappings
from crc_checker.file_types import TRACE_EXTENSIONS


@dataclass(frozen=True)
class ConfigIssue:
    """One diagnostic row: ``level`` is ``ok``, ``warning`` or ``error``."""

    level: str
    section: str
    message: str


def _add(issues: list[ConfigIssue], level: str, section: str,
         message: str) -> None:
    issues.append(ConfigIssue(level=level, section=section, message=message))


def _check_inputs(cfg: RunConfig, issues: list[ConfigIssue]) -> None:
    """DBC and trace paths: existence, types and channel-selection hints."""
    try:
        dbc_paths = cfg.get_dbc_paths()
    except (FileNotFoundError, ValueError) as exc:
        _add(issues, "error", "inputs", f"DBC: {exc}")
    else:
        if not dbc_paths:
            _add(issues, "error", "inputs",
                 "DBC: no .dbc files resolved from 'dbc'/'dbcs'")
        else:
            _add(issues, "ok", "inputs", f"DBC: {len(dbc_paths)} file(s)")
            if len(dbc_paths) > 1 and not cfg.dbc_merge:
                _add(issues, "warning", "inputs",
                     "multiple DBCs without dbc_merge: one DBC is selected per "
                     "trace channel (set dbc_merge to merge them into one map)")

    try:
        trace_paths = cfg.get_trace_paths()
    except (FileNotFoundError, ValueError) as exc:
        _add(issues, "error", "inputs", f"Traces: {exc}")
        return
    if not trace_paths:
        _add(issues, "error", "inputs", "Traces: no trace files resolved")
        return
    _add(issues, "ok", "inputs", f"Traces: {len(trace_paths)} file(s)")
    for path in trace_paths:
        suffix = Path(path).suffix.lower()
        if suffix not in TRACE_EXTENSIONS:
            supported = ", ".join(sorted(TRACE_EXTENSIONS))
            _add(issues, "error", "inputs",
                 f"Traces: unsupported extension '{suffix or '<none>'}' in "
                 f"{Path(path).name} (supported: {supported})")


def _check_dataid(cfg: RunConfig, issues: list[ConfigIssue]) -> None:
    """DataID workbook/JSON/mapping and the signal-pattern configuration.

    An existing ``dataid.json`` wins over the workbook at load time, so the
    workbook sheet/mapping are not required in that case (a missing sheet would
    otherwise be a false error the run itself never sees).
    """
    if cfg.dataid_json and Path(cfg.dataid_json).is_file():
        _add(issues, "ok", "dataid",
             f"dataid.json: {Path(cfg.dataid_json).name} "
             "(loaded instead of the workbook)")
    else:
        _check_dataid_sources(cfg, issues)
    _check_patterns(cfg, issues)


def _check_dataid_sources(cfg: RunConfig, issues: list[ConfigIssue]) -> None:
    """Checks that only apply when the DataID comes from the workbook."""
    if cfg.dataid_json:
        if cfg.dataid_xlsx:
            _add(issues, "ok", "dataid",
                 "dataid.json missing: the workbook is parsed on every run "
                 "(run 'canvouch preprocess-dataid' to cache it)")
        else:
            _add(issues, "error", "dataid",
                 f"dataid.json not found: {cfg.dataid_json}")
    if cfg.dataid_xlsx:
        if not Path(cfg.dataid_xlsx).is_file():
            _add(issues, "error", "dataid",
                 f"dataid.xlsx not found: {cfg.dataid_xlsx}")
        else:
            _check_workbook(cfg, issues)
    _check_named_mapping(cfg, issues)

    if cfg.dataid_mapping_file:
        if not Path(cfg.dataid_mapping_file).is_file():
            _add(issues, "error", "dataid",
                 f"dataid.mapping_file not found: {cfg.dataid_mapping_file}")
        else:
            _add(issues, "ok", "dataid",
                 f"dataid.mapping_file: {Path(cfg.dataid_mapping_file).name}")


def _check_workbook(cfg: RunConfig, issues: list[ConfigIssue]) -> None:
    """Workbook readability + configured sheet names (no row parsing)."""
    # pylint: disable=import-outside-toplevel
    from crc_checker.dataid.parser import get_sheet_names

    try:
        sheets = get_sheet_names(cfg.dataid_xlsx)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Workbook libraries raise format-specific exceptions for corrupt or
        # password-protected files; the CLI must report it, not crash.
        _add(issues, "error", "dataid",
             f"dataid.xlsx could not be read: {exc}")
        return
    if not sheets:
        _add(issues, "error", "dataid", "dataid.xlsx has no worksheets")
        return
    # JSON null is a legal blank value in the loader (``if self.dataid_sheet``),
    # so it must not crash validation either.
    requested = [item.strip() for item in (cfg.dataid_sheet or "").split(",")
                 if item.strip()]
    if not requested:
        _add(issues, "ok", "dataid",
             f"dataid.xlsx: {len(sheets)} sheet(s), auto-detected per DBC")
        return
    missing = [name for name in requested if name not in sheets]
    for name in missing:
        _add(issues, "error", "dataid",
             f"dataid.sheet '{name}' not found in {Path(cfg.dataid_xlsx).name} "
             f"(available: {', '.join(sheets[:8])}"
             f"{'...' if len(sheets) > 8 else ''})")
    if not missing:
        _add(issues, "ok", "dataid",
             f"dataid.xlsx: sheet(s) {', '.join(requested)} present")


def _check_named_mapping(cfg: RunConfig, issues: list[ConfigIssue]) -> None:
    """A named ``dataid.mapping`` must exist in the (custom or default) table."""
    mapping = cfg.dataid_mapping
    if not isinstance(mapping, str) or not mapping:
        return
    try:
        mappings = load_dataid_mappings(cfg.dataid_mapping_file or None)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        _add(issues, "error", "dataid", f"dataid.mapping: {exc}")
        return
    if mapping not in mappings:
        _add(issues, "error", "dataid",
             f"dataid.mapping '{mapping}' is not defined (available: "
             f"{', '.join(sorted(mappings))})")
    else:
        _add(issues, "ok", "dataid", f"dataid.mapping: {mapping}")


def _check_patterns(cfg: RunConfig, issues: list[ConfigIssue]) -> None:
    """Compile the signal-name patterns, including inline regex overrides.

    Inline ``signals.checksum_pattern`` / ``rc_pattern`` / ``*_patterns`` are
    compiled by the same ``build_naming_patterns()`` call, so a malformed inline
    regex must fail here rather than on the first run.
    """
    if cfg.patterns_file and not Path(cfg.patterns_file).is_file():
        _add(issues, "error", "signals",
             f"signals.patterns_file not found: {cfg.patterns_file}")
        return
    if not (cfg.patterns_file or cfg.checksum_pattern or cfg.rc_pattern
            or cfg.checksum_patterns or cfg.rc_patterns):
        return
    try:
        cfg.build_naming_patterns()
    except (OSError, ValueError, TypeError, re.error) as exc:
        _add(issues, "error", "signals", f"signals: {exc}")
    else:
        detail = (f"signals.patterns_file: {Path(cfg.patterns_file).name}"
                  if cfg.patterns_file else "signals: inline patterns compiled")
        _add(issues, "ok", "signals", detail)


def _check_output(cfg: RunConfig, issues: list[ConfigIssue]) -> None:
    """Report-output directory must exist for file-producing formats."""
    if cfg.output_format not in ("html", "json") or not cfg.output_file:
        return
    parent = Path(cfg.output_file).parent
    if not parent.is_dir():
        _add(issues, "error", "output",
             f"output.file directory does not exist: {parent}")
    elif not os.access(parent, os.W_OK):
        _add(issues, "error", "output",
             f"output.file directory is not writable: {parent}")
    else:
        _add(issues, "ok", "output",
             f"output.file: {Path(cfg.output_file).name}")


def validate_config_file(config_path: str) -> list[ConfigIssue]:
    """Validate a config file without parsing any trace.

    Returns a list of :class:`ConfigIssue`; a config that cannot even be loaded
    yields a single ``error`` row for the ``config`` section.
    """
    issues: list[ConfigIssue] = []
    try:
        cfg = load_config(config_path)
    except (OSError, ValueError, TypeError) as exc:
        _add(issues, "error", "config", str(exc))
        return issues

    _add(issues, "ok", "config", f"JSON parsed: {Path(config_path).name}")
    for warning in cfg.warnings:
        _add(issues, "warning", "config", warning)

    _check_inputs(cfg, issues)
    _check_dataid(cfg, issues)
    _check_output(cfg, issues)
    return issues


def has_errors(issues: list[ConfigIssue]) -> bool:
    """True when at least one row is an error (drives the exit code)."""
    return any(issue.level == "error" for issue in issues)
