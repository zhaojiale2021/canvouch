"""Shared helpers for CLI config tests."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from cli.main import cli

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "example"


def example_config(tmp_path: Path, trace_name: str = "example.blf",
                   **overrides) -> str:
    """Write the example config with absolute paths (table output by default)."""
    data = json.loads((EXAMPLE / "config.json").read_text(encoding="utf-8"))
    data["dbc"] = str(EXAMPLE / "dbc")
    data["traces"] = [str(EXAMPLE / "trace" / trace_name)]
    data["dataid"]["json"] = str(EXAMPLE / "dbc" / "dataid.json")
    data["output"] = {"format": "table"}
    data.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def run_cli(*args: str):
    """Invoke the top-level CLI in-process."""
    return CliRunner().invoke(cli, list(args))
