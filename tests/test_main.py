from __future__ import annotations

import json
from io import StringIO
from unittest.mock import patch

from foresight_cli.cli import app
from foresight_cli.commands import system as system_commands
from foresight_cli.utils import output as out
from rich.console import Console
from typer.testing import CliRunner


def test_main_health_prints_status(capsys):
    import foresight_mcp.__main__ as main_module

    with (
        patch.object(main_module.sys, "argv", ["foresight-mcp", "--health"]),
        patch.object(main_module, "init_db"),
        patch.object(main_module, "get_system_status", return_value='{"status":"healthy"}'),
    ):
        main_module.main()

    assert capsys.readouterr().out.strip() == '{"status":"healthy"}'


def test_main_help_prints_usage(capsys):
    import foresight_mcp.__main__ as main_module

    with patch.object(main_module.sys, "argv", ["foresight-mcp", "--help"]):
        main_module.main()

    assert "Usage: foresight-mcp" in capsys.readouterr().out


def test_status_human_formats_payload_budget_weights_and_injection_health():
    result = {
        "status": "healthy",
        "database": "/tmp/test.db",
        "bank_id": "default",
        "user_id": "test-user",
        "memory_count": 12,
        "crisis_signals": 0,
        "stale_count": 2,
        "payload_budget": {
            "min_lane_chars": 40,
            "default_lane_weights": {
                "blocks": 0.15,
                "dynamic": 0.15,
                "memories": 0.5,
                "safety": 0.1,
                "static": 0.1,
            },
        },
        "injection_health": {
            "total_runs": 4,
            "runs_24h": 3,
            "avg_latency_ms": 21.5,
            "avg_memories_returned": 5.25,
            "avg_memories_fetched": 7.5,
            "fast_path_rate_pct": 75.0,
            "last_run_at": "2026-06-23T00:00:00+00:00",
        },
    }
    stderr_buffer = StringIO()

    with (
        patch("foresight_cli.commands.system.init_db"),
        patch("foresight_cli.commands.system.get_system_status", return_value=result),
        patch.object(out, "_stderr_console", Console(file=stderr_buffer, force_terminal=False, color_system=None)),
    ):
        out.configure(mode="human", color=False, pipe_safe=False, verbose=False)
        system_commands.status()

    rendered = stderr_buffer.getvalue()
    assert "Payload Budget" in rendered
    assert "blocks: 15%" in rendered
    assert "memories: 50%" in rendered
    assert "Injection Health" in rendered
    assert "Total Runs: 4" in rendered
    assert "Fast Path Rate: 75%" in rendered


def test_status_json_mode_emits_scriptable_status_payload():
    result = {
        "status": "healthy",
        "memory_count": 12,
        "stale_count": 2,
        "payload_budget": {
            "min_lane_chars": 40,
            "default_lane_weights": {"memories": 0.5},
        },
        "injection_health": {
            "total_runs": 4,
            "fast_path_rate_pct": 75.0,
        },
    }

    with (
        patch("foresight_cli.commands.system.init_db"),
        patch("foresight_cli.commands.system.get_system_status", return_value=json.dumps(result)),
    ):
        cli_result = CliRunner().invoke(app, ["--output", "json", "status"])

    assert cli_result.exit_code == 0
    parsed = json.loads(cli_result.stdout)
    assert parsed["payload_budget"]["default_lane_weights"]["memories"] == 0.5
    assert parsed["injection_health"]["total_runs"] == 4
