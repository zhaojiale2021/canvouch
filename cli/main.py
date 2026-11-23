"""CLI entry point — click-based command group."""

from __future__ import annotations

import json
import re
from pathlib import Path
import click

from crc_checker.config import DEFAULT_MAX_FRAMES, RunConfig
from crc_checker.file_types import TRACE_EXTENSIONS
from crc_checker.core.models import CRCParams
from crc_checker.crc.presets import get_preset, get_validation, list_presets
from crc_checker.dbc.parser import parse_dbc
from crc_checker import __version__
from cli.output import (
    _exit_after_reports,
    _output_report,
    _print_json_reports,
    _report_output_path,
)


# ---------------------------------------------------------------------------
# Shared helpers used by both ``verify`` and ``run`` commands
# ---------------------------------------------------------------------------

_TRACE_EXTENSIONS = TRACE_EXTENSIONS


class GroupedCommand(click.Command):
    """Display related command options under named headings."""

    _OPTION_GROUPS = (
        ("Inputs", {"dbc_paths", "trace_files", "config_path", "dbc_merge"}),
        ("CRC and signal settings", {
            "algo", "custom_crc", "range_mode", "range_start", "range_end",
            "checksum_pattern", "rc_pattern", "patterns_file", "byte_swap",
            "auto_classify", "profile_by_width", "overrides", "profiles",
            "skip", "rc_max_valid",
        }),
        ("DataID", {
            "dataid_xlsx", "dataid_sheet", "dataid_mapping", "dataid_json",
            "dataid_mapping_file", "dataid_position", "dataid_byte_order",
            "dataid_default",
        }),
        ("Filters", {"frame_ids", "max_frames"}),
        ("Output", {
            "output_fmt", "output_file", "json_file", "only_failures",
            "show_all", "group_by", "max_report_sections",
        }),
        ("CI gates", {"fail_on_failure", "min_pass_rate", "allow_empty", "allow_truncated"}),
    )

    def format_options(self, ctx, formatter):
        """Write grouped options while retaining Click's standard formatting."""
        params = self.get_params(ctx)
        assigned = set()
        for heading, names in self._OPTION_GROUPS:
            records = [
                record for param in params if param.name in names
                if (record := param.get_help_record(ctx)) is not None
            ]
            if records:
                formatter.write_heading(heading)
                with formatter.indentation():
                    formatter.write_dl(records)
                assigned.update(names)
        remaining = [
            record for param in params if param.name not in assigned
            if (record := param.get_help_record(ctx)) is not None
        ]
        if remaining:
            formatter.write_heading("Other options")
            with formatter.indentation():
                formatter.write_dl(remaining)


def _parse_crc_params(algo: str | None, custom_data: dict | None) -> CRCParams | None:
    """Resolve CRC parameters from ``--algo`` or parsed ``--custom-crc`` JSON."""
    # pylint: disable=import-outside-toplevel
    from crc_checker.crc.custom import parse_custom_crc

    if custom_data:
        return parse_custom_crc(custom_data)
    if algo:
        return get_preset(algo)
    return None


def _load_custom_crc(custom_crc: str | None) -> dict | None:
    """Parse ``--custom-crc`` JSON once (both the params and the config use it)."""
    if not custom_crc:
        return None
    try:
        data = json.loads(custom_crc)
    except json.JSONDecodeError as exc:
        raise click.BadParameter(f"Invalid --custom-crc JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise click.BadParameter("--custom-crc must be a JSON object.")
    return data


def _parse_frame_ids(raw: str | None) -> list[int] | None:
    """Parse ``--frame-ids`` (hex ``0x..`` or decimal, separated by space/comma/semicolon)."""
    if not raw:
        return None
    try:
        return [
            int(token, 16) if token.lower().startswith("0x") else int(token)
            for token in re.split(r"[\s,;]+", raw.strip()) if token
        ]
    except ValueError as exc:
        raise click.BadParameter(f"Invalid --frame-ids value: {raw}") from exc


def _parse_json_object(raw: str | None, label: str) -> dict | None:
    """Parse an optional JSON object option (``--overrides`` / ``--profiles``)."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise click.BadParameter(f"Invalid {label} JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise click.BadParameter(f"{label} must be a JSON object.")
    return data


def _parse_hex_int(raw: str | None, label: str) -> int | None:
    """Parse a decimal/hex integer option."""
    if raw is None or raw == "":
        return None
    try:
        return int(raw, 0)
    except ValueError as exc:
        raise click.BadParameter(f"Invalid {label} value: {raw}") from exc


def _parse_names(raw: str | None) -> list[str]:
    """Split a comma/space separated list of frame names/IDs for the skip list."""
    if not raw:
        return []
    return [token for token in re.split(r"[\s,;]+", raw.strip()) if token]


def _expand_trace_paths(trace_files: list[str]) -> list[str]:
    """Expand trace file paths, recursing into directories."""
    result: list[str] = []
    for t in trace_files:
        tp = Path(t)
        if tp.is_dir():
            files = sorted(f for f in tp.iterdir()
                          if f.suffix.lower() in _TRACE_EXTENSIONS)
            if not files:
                raise click.BadParameter(f"No trace files found in: {t}")
            result.extend(str(f) for f in files)
        else:
            result.append(str(tp))
    return result


def _echo_verbose_config(cfg, config_path: str | None, trace_paths: list[str],
                         *, err: bool = False) -> None:
    """Print the resolved run inputs (shared by ``run`` and ``verify``)."""
    def out(text: str) -> None:
        click.echo(text, err=err)

    if config_path:
        out(f"Config: {config_path}")
    if cfg.dbc:
        out(f"DBC:    {cfg.dbc}")
    if cfg.dbcs:
        out(f"DBCs:   {', '.join(cfg.dbcs)}")
    out(f"Traces: {len(trace_paths)} file(s)")
    if cfg.crc_preset:
        out(f"CRC:    {cfg.crc_preset}")
    elif cfg.crc_custom:
        out("CRC:    custom")
    if cfg.dataid_json and Path(cfg.dataid_json).exists():
        out(f"DataID JSON: {cfg.dataid_json} (fast path)")
    elif cfg.dataid_xlsx:
        out(f"DataID xlsx: {cfg.dataid_xlsx}")


def _echo_verbose_setup(setup, trace_paths: list[str], *, err: bool = False,
                        include_protection_count: bool = False) -> None:
    """Print DBC selection notes and the trace list (shared by both commands)."""
    def out(text: str) -> None:
        click.echo(text, err=err)

    for note in setup.notes:
        out(note)
    if include_protection_count:
        out("Found protection signals in "
            f"{len(set(setup.protection_map.messages))} CAN IDs")
    out(f"Trace files: {len(trace_paths)} file(s)")
    for path in trace_paths:
        out(f"  {path}")


@click.group()
@click.version_option(__version__, prog_name="canvouch")
def cli():
    """CanVouch - DBC-aware CAN bus CRC/Rolling Counter verification tool."""


@cli.command(cls=GroupedCommand)
@click.option("--dbc", "dbc_paths", multiple=True,
              type=click.Path(exists=True), help="Path to DBC file(s) or directory (repeatable)")
@click.option("--trace", "trace_files", required=True, multiple=True,
              type=click.Path(exists=True), help="Path to trace file(s) (BLF, ASC, MF4, ...)")
@click.option("--algo", default=None, help="CRC preset name (e.g. CRC8/AUTOSAR)")
@click.option("--custom-crc", default=None, help="Custom CRC params as JSON string")
@click.option("--range", "range_mode", default=None,
              type=click.Choice(["full_payload", "exclude_crc", "custom_range"]),
              help="CRC data range mode (required when specifying range bounds)")
@click.option("--range-start", type=int, default=None, help="Custom range start byte (0-indexed)")
@click.option("--range-end", type=int, default=None,
              help="Custom range end byte (exclusive; -1 means payload end)")
@click.option("--checksum-pattern", default=None,
              help="Override regex for checksum signal matching")
@click.option("--rc-pattern", default=None,
              help="Override regex for rolling counter signal matching")
@click.option("--patterns-file", default=None, type=click.Path(exists=True),
              help="YAML with signal-name patterns (see crc_checker/data/patterns.yaml)")
@click.option("--byte-swap", is_flag=True,
              help="Byte-swap computed CRC before comparison (Profile 5)")
@click.option("--auto-classify", is_flag=True,
              help="Pick the CRC algorithm per message from DBC comments")
@click.option("--profile-by-width", is_flag=True,
              help="Select the E2E profile from the CRC signal width")
@click.option("--overrides", default=None,
              help="JSON object mapping frame id/name to a preset, e.g. "
                   '{"0x123":"CRC8/SAE-J1850"}')
@click.option("--profiles", default=None,
              help="JSON object with per-profile overrides (crc.profiles)")
@click.option("--skip", default=None,
              help="Comma-separated frame ids/names or signals to leave unverified")
@click.option("--rc-max-valid", type=int, default=None,
              help="Maximum valid rolling-counter value (default: 2^bits - 1)")
@click.option("--dataid-xlsx", default=None, type=click.Path(exists=True),
              help="Excel file with DataID mappings for E2E CRC calculation")
@click.option("--dataid-json", default=None, type=click.Path(),
              help="Preprocessed DataID JSON (fast path; may be generated from --dataid-xlsx)")
@click.option("--dataid-sheet", default=None,
              help="Sheet name in the DataID Excel (default: auto-detect)")
@click.option("--dataid-mapping", default=None,
              help="Column mapping: name from dataid_mappings.yaml "
                   "(hi_lo_two_cols, single_16bit, generic_abc, simple_8bit, "
                   "description_regex) or a JSON object")
@click.option("--dataid-mapping-file", default=None, type=click.Path(exists=True),
              help="Custom DataID mapping YAML")
@click.option("--dataid-position", default=None,
              type=click.Choice(["prepend", "append"]),
              help="Where the DataID enters the CRC")
@click.option("--dataid-byte-order", default=None,
              type=click.Choice(["lo_hi", "hi_lo"]),
              help="DataID byte order")
@click.option("--dataid-default", default=None,
              help="Fallback DataID when a message has no table entry (e.g. 0x0000)")
@click.option("--dbc-merge", is_flag=True,
              help="Merge every DBC into one map instead of selecting per channel")
@click.option("--frame-ids", default=None,
              help="Comma-separated CAN IDs to filter (e.g. 0x323,0x299)")
@click.option("--output", "output_fmt", default="table",
              type=click.Choice(["table", "html", "json"]),
              help="Output format")
@click.option("--output-file", default=None, type=click.Path(), help="Output file path")
@click.option("--only-failures", is_flag=True,
              help="Only show frames with failed verdicts. Table output prints "
                   "just the failed verdicts; HTML output keeps the failing "
                   "frame's passing verdicts too for context")
@click.option("--show-all", is_flag=True,
              help="Table output: print every raw verdict instead of the "
                   "per-signal aggregate")
@click.option("--group-by", "group_by", default="signal",
              type=click.Choice(["signal", "frame"]), show_default=True,
              help="Table aggregation when --show-all is not set")
@click.option("--max-report-sections", type=int, default=None,
              help="HTML report frame-section cap (0 = unlimited, default 30000)")
@click.option("--max-frames", type=int, default=DEFAULT_MAX_FRAMES,
              help="Maximum frames to process (0 = unlimited, default 6000)")
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
@click.option("--fail-on-failure", is_flag=True,
              help="Exit 1 if any check fails (use in CI)")
@click.option("--min-pass-rate", type=click.FloatRange(0, 100), default=None,
              help="Exit 1 when the aggregate pass rate is below this percentage")
@click.option("--allow-empty", is_flag=True,
              help="Allow a CI gate to pass when no checks were performed")
@click.option("--allow-truncated", is_flag=True,
              help="Allow a CI gate to pass when a trace hit its frame cap")
@click.option("--json-file", default=None, type=click.Path(),
              help="Write JSON output to this file (default: standard output)")
# pylint: disable=too-many-arguments,too-many-positional-arguments
# pylint: disable=too-many-locals,too-many-branches,too-many-statements
# pylint: disable=import-outside-toplevel
def verify(dbc_paths, trace_files, algo, custom_crc,
           range_mode, range_start, range_end,
           checksum_pattern, rc_pattern, patterns_file,
           byte_swap, auto_classify, profile_by_width, overrides, profiles,
           skip, rc_max_valid,
           dataid_xlsx, dataid_json, dataid_sheet, dataid_mapping,
           dataid_mapping_file, dataid_position, dataid_byte_order, dataid_default,
           dbc_merge, frame_ids, output_fmt, output_file,
           only_failures, show_all, group_by, max_report_sections, max_frames,
           verbose, fail_on_failure, min_pass_rate, allow_empty,
           allow_truncated, json_file):
    """Verify CRC and Rolling Counter in CAN trace files.

    This builds a RunConfig and calls the same ``run_config`` pipeline as
    ``canvouch run`` and the web API, so all three cannot drift apart.
    """
    from crc_checker.verify.runner import RunnerError, run_config

    if not dbc_paths:
        raise click.UsageError("At least one --dbc path is required.")

    if not algo and not custom_crc and not auto_classify:
        click.echo("Warning: No CRC algorithm specified. Use --algo, "
                   "--custom-crc or --auto-classify.", err=True)
        click.echo("Only Rolling Counter verification will be performed.", err=True)
    if algo and custom_crc:
        raise click.UsageError("Use only one of --algo and --custom-crc.")
    if rc_max_valid is not None and rc_max_valid < 0:
        raise click.BadParameter("--rc-max-valid must be >= 0.")
    if max_report_sections is not None and max_report_sections < 0:
        raise click.BadParameter("--max-report-sections must be >= 0.")
    dataid_default_value = _parse_hex_int(dataid_default, "--dataid-default")
    if dataid_default_value is not None and not 0 <= dataid_default_value <= 0xFFFF:
        raise click.BadParameter("--dataid-default must be a 16-bit value.")

    if (range_start is not None or range_end is not None) and range_mode != "custom_range":
        raise click.UsageError(
            "--range-start/--range-end require --range custom_range."
        )
    if range_start is not None and range_start < 0:
        raise click.BadParameter("--range-start must be >= 0.")
    if range_end is not None and range_end < -1:
        raise click.BadParameter("--range-end must be >= -1.")
    effective_start = range_start if range_start is not None else 0
    effective_end = range_end if range_end is not None else -1
    if 0 <= effective_end <= effective_start:
        raise click.BadParameter(
            "--range-end must be greater than --range-start (exclusive).")
    if json_file and output_fmt != "json":
        raise click.UsageError("--json-file requires --output json.")
    if output_file and json_file:
        raise click.UsageError("Use only one of --output-file and --json-file.")

    custom_data = _load_custom_crc(custom_crc)
    try:
        crc_params = _parse_crc_params(algo, custom_data)
    except (KeyError, TypeError, ValueError) as exc:
        raise click.BadParameter(f"Invalid CRC algorithm: {exc}") from exc
    if algo:
        status, _note = get_validation(algo)
        if status != "validated":
            click.echo(f"Warning: preset {algo} is not verified with real data.",
                       err=True)

    cfg = RunConfig()
    cfg.dbcs = list(dbc_paths)
    # Expand directories up front: DBC selection samples trace files, so it
    # must never be handed a directory.
    cfg.traces = _expand_trace_paths(list(trace_files))
    cfg.dbc_merge = dbc_merge
    cfg.crc_preset = algo or ""
    cfg.crc_custom = custom_data
    cfg.crc_byte_swap = byte_swap
    cfg.crc_auto_classify = auto_classify
    cfg.crc_profile_by_width = profile_by_width
    cfg.crc_overrides = _parse_json_object(overrides, "--overrides") or {}
    cfg.crc_profiles = _parse_json_object(profiles, "--profiles") or {}
    cfg.crc_skip = _parse_names(skip)
    cfg.rc_max_valid = rc_max_valid
    cfg.range_mode = range_mode or "exclude_crc"
    cfg.range_start = range_start if range_start is not None else 0
    cfg.range_end = range_end if range_end is not None else -1
    cfg.checksum_pattern = checksum_pattern
    cfg.rc_pattern = rc_pattern
    cfg.patterns_file = patterns_file or ""
    cfg.dataid_xlsx = dataid_xlsx or ""
    cfg.dataid_json = dataid_json or ""
    cfg.dataid_sheet = dataid_sheet or ""
    cfg.dataid_mapping = dataid_mapping or ""
    cfg.dataid_mapping_file = dataid_mapping_file or ""
    cfg.dataid_position = dataid_position or "prepend"
    cfg.dataid_byte_order = dataid_byte_order or "lo_hi"
    cfg.dataid_default = dataid_default_value
    cfg.frame_ids = _parse_frame_ids(frame_ids)
    cfg.max_frames = max_frames
    cfg.output_format = output_fmt
    cfg.output_file = output_file or ""
    cfg.only_failures = only_failures
    cfg.max_report_sections = max_report_sections

    if verbose:
        _echo_verbose_config(cfg, None, cfg.traces, err=output_fmt == "json")
        if crc_params:
            click.echo(f"CRC Algorithm: {crc_params.name}",
                       err=output_fmt == "json")
            click.echo(f"  poly=0x{crc_params.polynomial:X} "
                       f"init=0x{crc_params.initial_value:X} "
                       f"xor=0x{crc_params.final_xor:X} width={crc_params.width}",
                       err=output_fmt == "json")

    try:
        setup, reports = run_config(cfg, cfg.traces)
    except (RunnerError, ValueError, OSError, KeyError, re.error) as exc:
        raise click.UsageError(str(exc)) from exc
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Corrupt workbooks / optional readers can raise types outside the
        # expected list. Keep the CLI free of tracebacks unless -v asks for one.
        if verbose:
            raise
        raise click.UsageError(f"Verification failed: {exc}") from exc

    if verbose:
        _echo_verbose_setup(setup, cfg.traces, err=output_fmt == "json",
                            include_protection_count=True)

    if output_fmt == "json":
        _print_json_reports(reports, json_file or output_file, only_failures)
    else:
        multiple_reports = len(reports) > 1
        for index, report in enumerate(reports, start=1):
            _output_report(report, output_fmt,
                           _report_output_path(output_file, report, multiple_reports,
                                               index=index),
                           only_failures,
                           cfg.max_report_sections,
                           show_all=show_all, group_by=group_by)
    _exit_after_reports(reports, fail_on_failure, min_pass_rate,
                        allow_empty, allow_truncated)


@cli.command("presets")
def list_presets_cmd():
    """List all available CRC algorithm presets."""
    click.echo("Available CRC presets:")
    click.echo()
    for name in list_presets():
        p = get_preset(name)
        status, note = get_validation(name)
        label = ("verified with real data" if status == "validated"
                 else "NOT verified with real data")
        click.echo(
            f"  {name:20s} width={p.width} poly=0x{p.polynomial:0{(p.width+3)//4}X} "
            f"init=0x{p.initial_value:0{(p.width+3)//4}X} "
            f"xor=0x{p.final_xor:0{(p.width+3)//4}X} "
            f"ref_in={int(p.reflect_in)} ref_out={int(p.reflect_out)}  "
            f"[{label}]"
        )
        if status != "validated" and note:
            click.echo(f"  {'':20s} {note}")


@cli.command("run", cls=GroupedCommand)
@click.option("--config", "-c", "config_path", required=True,
              type=click.Path(exists=True), help="Path to JSON config file")
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
@click.option("--show-all", is_flag=True,
              help="Table output: print every raw verdict instead of the "
                   "per-signal aggregate")
@click.option("--group-by", "group_by", default="signal",
              type=click.Choice(["signal", "frame"]), show_default=True,
              help="Table aggregation when --show-all is not set")
@click.option("--fail-on-failure", is_flag=True,
              help="Exit 1 if any check fails (use in CI)")
@click.option("--min-pass-rate", type=click.FloatRange(0, 100), default=None,
              help="Exit 1 when the aggregate pass rate is below this percentage")
@click.option("--allow-empty", is_flag=True,
              help="Allow a CI gate to pass when no checks were performed")
@click.option("--allow-truncated", is_flag=True,
              help="Allow a CI gate to pass when a trace hit its frame cap")
# pylint: disable=too-many-branches,too-many-statements,too-many-arguments,too-many-positional-arguments,too-many-locals
def run_cmd(config_path, verbose, show_all, group_by, fail_on_failure,
            min_pass_rate, allow_empty, allow_truncated):
    """Run verification from a JSON config file.

    Generate an example config:  canvouch gen-config example.json
    Then edit and run:           canvouch run -c example.json
    """
    # pylint: disable=import-outside-toplevel
    from crc_checker.config import load_config
    from crc_checker.verify.runner import RunnerError, run_config

    try:
        cfg = load_config(config_path)
    except (OSError, ValueError) as exc:
        raise click.BadParameter(f"Failed to read config file: {exc}") from exc

    for warning in cfg.warnings:
        click.echo(f"Config warning: {warning}", err=cfg.output_format == "json")

    if verbose:
        _echo_verbose_config(cfg, config_path, cfg.traces,
                             err=cfg.output_format == "json")

    if not cfg.crc_preset and not cfg.crc_custom and not cfg.crc_auto_classify:
        click.echo("Warning: No CRC algorithm configured. RC-only verification.",
                   err=True)
    if cfg.crc_preset:
        try:
            status, _note = get_validation(cfg.crc_preset)
        except KeyError as exc:
            raise click.BadParameter(str(exc)) from exc
        if status != "validated":
            click.echo(f"Warning: preset {cfg.crc_preset} is not verified with "
                       "real data.", err=True)

    try:
        trace_paths = cfg.get_trace_paths()
    except FileNotFoundError as exc:
        raise click.BadParameter(str(exc)) from exc

    # Everything below (DBC selection → DataID → CRC config → verify) lives in
    # crc_checker.verify.runner so the web API cannot drift from the CLI.
    try:
        setup, reports = run_config(cfg, trace_paths)
    except (RunnerError, ValueError, OSError, KeyError, re.error) as exc:
        raise click.UsageError(str(exc)) from exc
    except Exception as exc:  # pylint: disable=broad-exception-caught
        if verbose:
            raise
        raise click.UsageError(f"Verification failed: {exc}") from exc

    if verbose:
        _echo_verbose_setup(setup, trace_paths,
                            err=cfg.output_format == "json")

    if cfg.output_format == "json":
        _print_json_reports(reports, cfg.output_file or None, cfg.only_failures)
    else:
        multiple_reports = len(reports) > 1
        for index, report in enumerate(reports, start=1):
            _output_report(report, cfg.output_format,
                           _report_output_path(cfg.output_file, report, multiple_reports,
                                               index=index),
                           cfg.only_failures, cfg.max_report_sections,
                           show_all=show_all, group_by=group_by)
    _exit_after_reports(reports, fail_on_failure, min_pass_rate,
                        allow_empty, allow_truncated)


@cli.command("preprocess-dataid")
@click.option("--xlsx", required=True, type=click.Path(exists=True),
              help="Path to DataID Excel file")
@click.option("--sheet", "sheets", multiple=True, default=None,
              help="Sheet name(s) to parse (repeatable; default: all sheets)")
@click.option("--mapping", default="hi_lo_two_cols",
              help="Column mapping: name from dataid_mappings.yaml "
                   "(hi_lo_two_cols, single_16bit, generic_abc, simple_8bit, "
                   "description_regex) or a JSON object")
@click.option("--output", "-o", required=True, type=click.Path(),
              help="Output JSON file path")
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
# pylint: disable=import-outside-toplevel
def preprocess_dataid(xlsx, sheets, mapping, output, verbose):
    """Preprocess DataID Excel into standardized JSON for fast reload.

    Parse one or more sheets from a DataID Excel file and export a
    unified JSON that subsequent 'canvouch run' can load directly,
    skipping the slow openpyxl step.

    \b
    Example:
      canvouch preprocess-dataid --xlsx dataid.xlsx \
          --sheet "Sheet1" --sheet "Sheet2" -o dataid.json
    """
    from crc_checker.dataid import parse_dataid_xlsx_multi

    sheet_list = list(sheets) if sheets else None

    if verbose:
        click.echo(f"Parsing: {xlsx}")
        click.echo(f"Mapping: {mapping}")
        if sheet_list:
            click.echo(f"Sheets: {', '.join(sheet_list)}")
        else:
            click.echo("Sheets: all")

    try:
        dm = parse_dataid_xlsx_multi(xlsx, sheet_list, mapping=mapping)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise click.BadParameter(str(exc)) from exc
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Excel libraries raise format-specific exceptions for corrupt files.
        raise click.BadParameter(f"DataID workbook could not be parsed: {exc}") from exc

    if len(dm) == 0:
        click.echo("Warning: No DataID entries found.", err=True)

    parsed_sheets = sheet_list or ["<auto>"]
    dm.to_json(output, source=xlsx, mapping=mapping, sheets=parsed_sheets)
    click.echo(f"DataID JSON written to {output} ({len(dm)} entries)")


@cli.command("gen-config")
@click.argument("output_path", type=click.Path(), default="crc_config.json")
# pylint: disable=import-outside-toplevel
def gen_config_cmd(output_path):
    """Generate an example JSON config file."""
    from crc_checker.config import generate_example_config
    generate_example_config(output_path)
    click.echo(f"Example config written to {output_path}")
    click.echo(f"Edit it, then: canvouch run -c {output_path}")


@cli.command("validate-config")
@click.argument("config_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--quiet", is_flag=True, help="Only print warnings and errors")
# pylint: disable=import-outside-toplevel
def validate_config_cmd(config_path, quiet):
    """Check a JSON config without parsing any trace file.

    Resolves every configured path, checks the workbook sheets and named
    mappings, compiles the signal-name patterns and reports per-section
    diagnostics. Exits non-zero when anything would fail the run.
    """
    from crc_checker.config_check import has_errors, validate_config_file

    issues = validate_config_file(config_path)
    colors = {"ok": "green", "warning": "yellow", "error": "red"}
    labels = {"ok": "OK", "warning": "WARN", "error": "ERROR"}
    for issue in issues:
        if quiet and issue.level == "ok":
            continue
        label = click.style(f"[{labels[issue.level]}]", fg=colors[issue.level])
        click.echo(f"{label} {issue.section}: {issue.message}")
    errors = sum(1 for issue in issues if issue.level == "error")
    warnings = sum(1 for issue in issues if issue.level == "warning")
    if has_errors(issues):
        click.echo(f"Config invalid: {errors} error(s), {warnings} warning(s).",
                   err=True)
        raise click.exceptions.Exit(1)
    click.echo(f"Config OK ({warnings} warning(s)).")


# pylint: disable=too-many-locals
def _run_explain(config_path, trace_files):
    """Load a config, resolve it and print every group's effective settings."""
    # pylint: disable=import-outside-toplevel
    from crc_checker.config import load_config
    from crc_checker.verify.runner import RunnerError, build_protection_map

    try:
        cfg = load_config(config_path)
    except (OSError, ValueError, TypeError) as exc:
        raise click.BadParameter(f"Failed to read config file: {exc}") from exc
    for warning in cfg.warnings:
        click.echo(f"Config warning: {warning}", err=True)

    trace_paths: list[str] | None = list(trace_files) or None
    trace_error: FileNotFoundError | None = None
    if trace_paths is None:
        try:
            trace_paths = cfg.get_trace_paths()
        except FileNotFoundError as exc:
            # One DBC needs no trace; several DBCs do (channel selection).
            trace_paths = None
            trace_error = exc
    try:
        setup = build_protection_map(cfg, trace_sample=trace_paths,
                                     collect_diagnostics=True)
    except RunnerError as exc:
        # Several DBCs and no usable trace: surface the real path error
        # instead of the generic "need a trace sample" message.
        if trace_error is not None and "Need a trace file" in str(exc):
            raise click.UsageError(str(trace_error)) from exc
        raise click.UsageError(str(exc)) from exc
    except (ValueError, OSError, KeyError, TypeError, re.error) as exc:
        raise click.UsageError(str(exc)) from exc

    click.echo(f"Resolved configuration: {config_path}")
    for note in setup.notes:
        click.echo(f"  {note}")
    click.echo()

    groups = sorted(
        setup.resolved_groups,
        key=lambda group: (
            group.channel if group.channel is not None else -1,
            group.frame_id, group.pair_suffix, group.checksum_signal))
    for group in groups:
        channel = ("fallback" if group.channel is None
                   else f"CH{group.channel}")
        title = f"0x{group.frame_id:03X} {group.frame_name}"
        if group.pair_suffix:
            title += f" [{group.pair_suffix}]"
        click.echo(f"{title}  ({channel})")
        checksum = (f"{group.checksum_signal} ({group.checksum_bits} bits)"
                    if group.checksum_signal else "-")
        click.echo(f"  checksum:  {checksum}")
        click.echo(f"  algorithm: {group.algorithm or '-'}   "
                   f"profile: {group.profile or '-'}")
        dataid = (f"0x{group.dataid:04X}"
                  if group.dataid is not None else "-")
        source = f" ({group.dataid_source})" if group.dataid_source else ""
        click.echo(f"  range:     {group.range_text}   "
                   f"DataID: {dataid}{source}")
        rc_limit = (f"0..{group.rc_limit}"
                    if group.rc_limit is not None else "-")
        click.echo(f"  counter:   {group.rc_signal or '-'}  {rc_limit}")
        if group.skip_reason:
            click.echo(click.style(
                f"  status:    {group.status}", fg="yellow"))
        else:
            click.echo(f"  status:    {group.status}")
        click.echo()

    skipped = sum(1 for group in groups if group.skipped)
    dropped = sum(1 for group in groups if group.checksum_dropped)
    click.echo(f"{len(groups)} protection group(s), {skipped} skipped, "
               f"{dropped} checksum(s) dropped.")


@cli.command("explain")
@click.option("--config", "-c", "config_path", required=True,
              type=click.Path(exists=True, dir_okay=False),
              help="Path to JSON config file")
@click.option("--trace", "trace_files", multiple=True,
              type=click.Path(exists=True),
              help="Trace file(s) used only to pick a DBC per channel")
def explain_cmd(config_path, trace_files):
    """Show the fully resolved configuration for every protection group.

    ``inspect <dbc>`` lists DBC signal positions; this view (also available as
    ``inspect --config``) prints the algorithm, E2E profile, CRC range, DataID
    value/source, counter limit and skip reason that a run would actually use.
    """
    _run_explain(config_path, trace_files)


@cli.command()
@click.argument("dbc_path", required=False, type=click.Path(exists=True))
@click.option("--config", "config_path",
              type=click.Path(exists=True, dir_okay=False),
              help="Show the resolved configuration from a JSON config instead")
@click.option("--trace", "trace_files", multiple=True,
              type=click.Path(exists=True),
              help="With --config: trace sample(s) for per-channel DBC selection")
# pylint: disable=import-outside-toplevel
def inspect(dbc_path, config_path, trace_files):
    """Inspect a DBC file (or directory of DBCs) and list detected signals.

    With ``--config``, print the configuration every protection group will
    actually use (same view as ``canvouch explain``).
    """
    if config_path:
        if dbc_path:
            raise click.UsageError("Use either a DBC path or --config, not both.")
        _run_explain(config_path, trace_files)
        return
    if not dbc_path:
        raise click.UsageError("Provide a DBC path (or --config).")

    # Same expansion rule as verify's --dbc: a directory means every *.dbc
    # directly inside it.
    path = Path(dbc_path)
    if path.is_dir():
        dbc_files = sorted(path.glob("*.dbc"))
        if not dbc_files:
            raise click.UsageError(f"No .dbc files found in: {dbc_path}")
    else:
        dbc_files = [path]

    for dbc_file in dbc_files:
        protection_map = parse_dbc(str(dbc_file))
        if not protection_map.all_frame_ids():
            click.echo(f"{dbc_file.name}: no protection signals "
                       "(Checksum/RollingCounter) found.")
            continue
        click.echo(f"DBC: {dbc_file.name}")
        click.echo(f"Found protection signals in "
                   f"{len(protection_map.all_frame_ids())} CAN IDs:\n")

        for frame_id in sorted(protection_map.all_frame_ids()):
            for prot in protection_map.get_protections(frame_id):
                click.echo(f"  ID=0x{frame_id:03X} ({prot.frame_name}) "
                           f"[{prot.pair_suffix}]")
                if prot.checksum_signal:
                    cs = prot.checksum_signal
                    click.echo(f"    Checksum:  {cs.name}  "
                               f"start_bit={cs.start_bit} length={cs.length}")
                if prot.rolling_counter_signal:
                    rc = prot.rolling_counter_signal
                    click.echo(f"    RC:        {rc.name}  "
                               f"start_bit={rc.start_bit} length={rc.length}")
                click.echo()


if __name__ == "__main__":
    cli()
