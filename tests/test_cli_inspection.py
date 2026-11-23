"""Tests for the v0.3 CLI inspection surface.

``validate-config`` (path/diagnostic checks without trace parsing),
``explain`` / ``inspect --config`` (resolved per-group configuration) and the
aggregated default table view.
"""

from __future__ import annotations

import json

from openpyxl import Workbook

from tests.config_helpers import (
    EXAMPLE,
    example_config as _example_config,
    run_cli as _run,
)


class TestValidateConfig:
    """Config diagnostics must fail before a trace run would."""

    def test_example_config_is_valid(self):
        """The shipped example validates without errors."""
        result = _run("validate-config", str(EXAMPLE / "config.json"))
        assert result.exit_code == 0, result.output
        assert "[OK] config: JSON parsed" in result.output
        assert "[OK] inputs: DBC:" in result.output
        assert "[OK] inputs: Traces:" in result.output
        assert "Config OK" in result.output

    def test_missing_dbc_is_an_error(self, tmp_path):
        """A missing DBC path is reported as an error, not deferred to the run."""
        config = _example_config(tmp_path, dbc=str(tmp_path / "missing.dbc"))
        result = _run("validate-config", config)
        assert result.exit_code == 1
        assert "[ERROR] inputs:" in result.output
        assert "not found" in result.output

    def test_invalid_json_exits_non_zero(self, tmp_path):
        """A malformed JSON file yields one clean config error."""
        config = tmp_path / "broken.json"
        config.write_text("{ not json", encoding="utf-8")
        result = _run("validate-config", str(config))
        assert result.exit_code == 1
        assert "[ERROR] config:" in result.output

    def test_unknown_key_is_a_warning_not_a_failure(self, tmp_path):
        """Typos are warnings so an otherwise valid config still runs."""
        config = _example_config(tmp_path, filtre={})
        result = _run("validate-config", config)
        assert result.exit_code == 0, result.output
        assert "[WARN] config:" in result.output
        assert "filtre" in result.output

    def test_unsupported_trace_extension_is_an_error(self, tmp_path):
        """An extension no reader dispatches is rejected up front."""
        trace = tmp_path / "trace.xyz"
        trace.write_text("not a trace", encoding="utf-8")
        config = _example_config(tmp_path, traces=[str(trace)])
        result = _run("validate-config", config)
        assert result.exit_code == 1
        assert "unsupported extension '.xyz'" in result.output

    def test_trace_contents_are_not_parsed(self, tmp_path):
        """Validation must not open the trace (garbage passes; a run would not)."""
        trace = tmp_path / "garbage.blf"
        trace.write_text("this is not an BLF file", encoding="utf-8")
        config = _example_config(tmp_path, traces=[str(trace)])
        result = _run("validate-config", config)
        assert result.exit_code == 0, result.output
        assert "Config OK" in result.output

    def test_configured_sheet_must_exist(self, tmp_path):
        """A configured sheet name is checked against the workbook."""
        workbook = tmp_path / "dataid.xlsx"
        book = Workbook()
        book.active.title = "Sheet1"
        book.save(workbook)
        config = _example_config(tmp_path, dataid={
            "xlsx": str(workbook),
            "sheet": "MissingSheet",
            "mapping": "hi_lo_two_cols",
        })
        result = _run("validate-config", config)
        assert result.exit_code == 1
        assert "dataid.sheet 'MissingSheet' not found" in result.output

    def test_null_sheet_does_not_crash(self, tmp_path):
        """``"sheet": null`` is accepted by the loader and must not crash here."""
        workbook = tmp_path / "dataid.xlsx"
        book = Workbook()
        book.active.title = "Sheet1"
        book.save(workbook)
        config = _example_config(tmp_path, dataid={
            "xlsx": str(workbook),
            "sheet": None,
            "mapping": "hi_lo_two_cols",
        })
        result = _run("validate-config", config)
        assert result.exit_code == 0, result.output
        assert "Config OK" in result.output

    def test_inline_regex_is_compiled(self, tmp_path):
        """Inline signal regexes are compiled, not only patterns_file regexes."""
        config = _example_config(
            tmp_path, signals={"checksum_pattern": "(?P<broken"})
        result = _run("validate-config", config)
        assert result.exit_code == 1
        assert "[ERROR] signals:" in result.output

    def test_missing_output_directory_is_an_error(self, tmp_path):
        """A report path under a missing directory fails before the run."""
        config = _example_config(tmp_path, output={
            "format": "html",
            "file": str(tmp_path / "does-not-exist" / "report.html"),
        })
        result = _run("validate-config", config)
        assert result.exit_code == 1
        assert "[ERROR] output:" in result.output

    def test_missing_patterns_file_is_an_error(self, tmp_path):
        """A configured patterns YAML must exist."""
        config = _example_config(tmp_path, signals={
            "patterns_file": str(tmp_path / "nope.yaml")})
        result = _run("validate-config", config)
        assert result.exit_code == 1
        assert "[ERROR] signals:" in result.output

    def test_unknown_dataid_mapping_is_an_error(self, tmp_path):
        """A named column mapping must exist in the mapping table."""
        workbook = tmp_path / "dataid.xlsx"
        book = Workbook()
        book.active.title = "Sheet1"
        book.save(workbook)
        config = _example_config(tmp_path, dataid={
            "xlsx": str(workbook),
            "mapping": "not_a_layout",
        })
        result = _run("validate-config", config)
        assert result.exit_code == 1
        assert "[ERROR] dataid: dataid.mapping 'not_a_layout'" in result.output

    def test_existing_json_wins_over_workbook_settings(self, tmp_path):
        """A pre-generated JSON is loaded instead of the workbook — do not
        flag its missing sheets or mapping as errors."""
        config = _example_config(tmp_path, dataid={
            "json": str(EXAMPLE / "dbc" / "dataid.json"),
            "xlsx": str(tmp_path / "missing.xlsx"),
            "sheet": "MissingSheet",
            "mapping": "not_a_layout",
        })
        result = _run("validate-config", config)
        assert result.exit_code == 0, result.output
        assert "[ERROR]" not in result.output

    def test_missing_dataid_json_without_workbook_is_an_error(self, tmp_path):
        """A missing JSON with no workbook to regenerate it is an error."""
        config = _example_config(tmp_path, dataid={
            "json": str(tmp_path / "missing.json"),
            "mapping": "hi_lo_two_cols",
        })
        result = _run("validate-config", config)
        assert result.exit_code == 1
        assert "dataid.json not found" in result.output

    def test_multiple_dbcs_without_merge_warns(self, tmp_path):
        """Per-channel selection is called out for multi-DBC configs."""
        dbc = str(EXAMPLE / "dbc" / "network.dbc")
        config = _example_config(tmp_path, dbc="", dbcs=[dbc, dbc])
        result = _run("validate-config", config)
        assert result.exit_code == 0, result.output
        assert "multiple DBCs without dbc_merge" in result.output

    def test_quiet_hides_ok_rows(self):
        """``--quiet`` keeps only warnings/errors."""
        result = _run("validate-config", "--quiet",
                      str(EXAMPLE / "config.json"))
        assert result.exit_code == 0, result.output
        assert "[OK]" not in result.output
        assert "Config OK" in result.output


class TestExplain:
    """The resolved-config view must show what the run will use."""

    def test_example_resolves_algorithm_range_and_dataid(self):
        """The example's algorithm, profile, range and DataID are all shown."""
        result = _run("explain", "-c", str(EXAMPLE / "config.json"))
        assert result.exit_code == 0, result.output
        output = result.output
        assert "Resolved configuration:" in output
        assert "0x100 MsgAlpha" in output
        assert "CRC8/SAE-J1850-ZERO" in output
        assert "profile: Profile 1" in output
        assert "DataID: 0x1234" in output
        assert "0..14" in output
        assert "status:    verify" in output

    def test_skip_reason_is_reported_per_group(self, tmp_path):
        """A skip-list entry is shown on its group and counted."""
        data = json.loads((EXAMPLE / "config.json").read_text(encoding="utf-8"))
        data["dbc"] = str(EXAMPLE / "dbc")
        data["traces"] = [str(EXAMPLE / "trace" / "example.blf")]
        data["dataid"]["json"] = str(EXAMPLE / "dbc" / "dataid.json")
        data["crc"]["skip"] = ["Checksum_Alpha"]
        config = tmp_path / "config.json"
        config.write_text(json.dumps(data), encoding="utf-8")

        result = _run("explain", "-c", str(config))
        assert result.exit_code == 0, result.output
        assert "skipped - skip list" in result.output
        assert "1 skipped" in result.output

    def test_checksum_dropped_keeps_the_counter_verified(self, tmp_path):
        """A comment-uncomputable checksum is dropped, not the whole group."""
        dbc = tmp_path / "external.dbc"
        dbc.write_text(
            'VERSION ""\n\nNS_ :\n    CM_\n\nBS_:\n\nBU_: ECU\n\n'
            "BO_ 256 MsgExt: 8 ECU\n"
            ' SG_ Ext_CRC : 7|8@0+ (1,0) [0|255] "" ECU\n'
            ' SG_ Ext_Counter : 15|4@0+ (1,0) [0|15] "" ECU\n\n'
            'CM_ SG_ 256 Ext_CRC "CRC8 算法另附文件";\n',
            encoding="utf-8")
        config = tmp_path / "config.json"
        config.write_text(json.dumps({
            "dbc": str(dbc),
            "traces": ["unused.blf"],
            "crc": {"auto_classify": True},
            "output": {"format": "table"},
        }), encoding="utf-8")

        result = _run("explain", "-c", str(config))
        assert result.exit_code == 0, result.output
        assert "RC still verified" in result.output
        assert "1 checksum(s) dropped" in result.output
        assert "0 skipped" in result.output

    def test_inspect_config_uses_the_same_view(self):
        """``inspect --config`` is an alias for the resolved view."""
        result = _run("inspect", "--config", str(EXAMPLE / "config.json"))
        assert result.exit_code == 0, result.output
        assert "Resolved configuration:" in result.output
        assert "0x200 MsgBeta" in result.output

    def test_multiple_dbcs_need_a_trace_sample(self, tmp_path):
        """The missing trace path is surfaced, not a generic DBC error."""
        dbc = str(EXAMPLE / "dbc" / "network.dbc")
        config = _example_config(
            tmp_path, dbc="", dbcs=[dbc, dbc],
            traces=[str(tmp_path / "missing.blf")])
        result = _run("explain", "-c", config)
        assert result.exit_code != 0
        assert "Trace path not found" in result.output


class TestAggregatedTable:
    """The default table is a summary; --show-all keeps raw verdicts."""

    @staticmethod
    def _verify(*extra: str):
        """Run the example with a deliberately wrong global CRC preset."""
        return _run(
            "verify", "--dbc", str(EXAMPLE / "dbc"),
            "--trace", str(EXAMPLE / "trace" / "example.blf"),
            "--algo", "CRC8/SAE-J1850", "--max-frames", "20", *extra)

    def test_default_view_is_grouped_by_signal(self):
        """The default view aggregates instead of printing every frame."""
        result = self._verify()
        assert result.exit_code == 0, result.output
        assert "Signal" in result.output
        assert "Checks" in result.output
        assert "Timestamp" not in result.output
        # 20 broken frames x 8 checks would be 160 raw rows; the aggregate is
        # one row per signal (8 rows here).
        assert result.output.count("FAIL") <= 4

    def test_group_by_frame_aggregates_signals(self):
        """``--group-by frame`` rolls the signal rows up per frame."""
        result = self._verify("--group-by", "frame")
        assert result.exit_code == 0, result.output
        assert "Signals" in result.output
        assert "1/2" in result.output

    def test_show_all_keeps_the_raw_rows(self):
        """``--show-all`` restores the one-row-per-verdict view."""
        result = self._verify("--show-all")
        assert result.exit_code == 0, result.output
        assert "Timestamp" in result.output
        assert "Expected" in result.output

    def test_only_failures_keeps_the_empty_message(self, tmp_path):
        """A clean failures-only run still prints the summary."""
        config = _example_config(tmp_path, output={"format": "table",
                                                   "only_failures": True})
        result = _run("run", "-c", config)
        assert result.exit_code == 0, result.output
        assert "No failed verdicts." in result.output


class TestReportSplitNotice:
    """Every written report part must be announced by the CLI."""

    # A single test method: the class groups the behaviour for the runner.
    # pylint: disable=too-few-public-methods

    def test_split_parts_are_printed(self, tmp_path, monkeypatch):
        """Every ``_pN.html`` part is announced and exists."""
        monkeypatch.setattr(
            "crc_checker.report.html.SPLIT_MAX_SECTIONS", 1)
        report = tmp_path / "report.html"
        config = _example_config(
            tmp_path,
            output={"format": "html", "file": str(report)},
            filter={"max_frames": 20},
        )
        result = _run("run", "-c", config)
        assert result.exit_code == 0, result.output
        assert f"HTML: {report}" in result.output
        assert "HTML part:" in result.output
        assert "report_p2.html" in result.output
        assert (tmp_path / "report_p2.html").is_file()
