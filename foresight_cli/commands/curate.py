"""Curation run management commands."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from foresight_mcp import CurationRunAction, manage_curation_runs
from foresight_mcp.server import init_db

from ..utils import config as cfg, output as out

app = typer.Typer(help="Manage async Foresight curation runs.")


def _init_and_user(user_id_override: str | None = None):
    init_db()
    return cfg.get_user_id(user_id_override)


def _coerce(value: str, allowed: set[str], label: str) -> str:
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise typer.BadParameter(f"{label} must be one of: {choices}")
    return value


def _emit(result: str) -> None:
    payload = json.loads(result) if isinstance(result, str) else result
    if isinstance(payload, dict) and not payload.get("ok", True):
        out.error(payload.get("error", {}).get("message", str(result)))
        raise typer.Exit(1)

    if out.get_settings().mode == "agent":
        out.print_json(payload)
        return

    if isinstance(payload, dict):
        if "run" in payload:
            out.print_json(payload["run"])
        elif "runs" in payload:
            runs = payload["runs"]
            if isinstance(runs, list) and runs:
                rows = []
                for r in runs:
                    rows.append(
                        [
                            r.get("id", "?")[:12],
                            r.get("status", "?"),
                            r.get("policy_mode", "?"),
                            r.get("source_bank_id", "?"),
                        ]
                    )
                out.print_table(["ID", "Status", "Policy", "Source Bank"], rows, title="Curation Runs")
            else:
                out.info("No curation runs found.")
        else:
            out.print_json(payload)
    else:
        out.stdout(str(payload))


@app.command()
def create(
    source_bank_id: str = typer.Option("default", "--source-bank-id", help="Source bank to curate"),
    output_bank_id: str | None = typer.Option(None, "--output-bank-id", help="Optional reviewable output bank"),
    policy_mode: str = typer.Option("rebalance", "--policy-mode", help="preserve, rebalance, or rebuild"),
    tool_access: str = typer.Option("observe", "--tool-access", help="disabled, observe, or operate"),
    output_mode: str = typer.Option("reviewable_output", "--output-mode", help="reviewable_output or in_place"),
    instructions: str | None = typer.Option(None, "--instructions", help="Optional curator instructions"),
    transcript_bundle_file: Path | None = typer.Option(
        None,
        "--transcript-bundle",
        help="JSON file with transcript messages",
        exists=True,
        dir_okay=False,
        readable=True,
    ),
    session_id: str | None = typer.Option(None, "--session-id", help="Transcript session ID"),
    run_clustering: bool = typer.Option(False, "--run-clustering", help="Run clustering after curation"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Create a new curation run."""
    _init_and_user(user_id)
    policy = _coerce(policy_mode, {"preserve", "rebalance", "rebuild"}, "policy-mode")
    access = _coerce(tool_access, {"disabled", "observe", "operate"}, "tool-access")
    mode = _coerce(output_mode, {"reviewable_output", "in_place"}, "output-mode")

    bundle = None
    if transcript_bundle_file:
        bundle = json.loads(transcript_bundle_file.read_text())

    result = manage_curation_runs(
        options=CurationRunAction(
            action="create",
            source_bank_id=source_bank_id,
            output_bank_id=output_bank_id,
            policy_mode=policy,
            tool_access=access,
            output_mode=mode,
            instructions=instructions,
            transcript_bundle=bundle,
            session_id=session_id,
            project_path=None,
        ),
    )
    _emit(result)


@app.command("get")
def get_run(
    run_id: str = typer.Argument(..., help="Curation run ID"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Get details of a curation run."""
    _init_and_user(user_id)
    result = manage_curation_runs(options=CurationRunAction(action="get", run_id=run_id))
    _emit(result)


@app.command("list")
def list_runs(
    limit: int = typer.Option(20, "--limit", "-l", help="Maximum number of runs"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """List recent curation runs."""
    _init_and_user(user_id)
    result = manage_curation_runs(options=CurationRunAction(action="list", limit=limit))
    _emit(result)


@app.command("cancel")
def cancel_cmd(
    run_id: str = typer.Argument(..., help="Curation run ID"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Cancel a pending or running curation run."""
    _init_and_user(user_id)
    result = manage_curation_runs(options=CurationRunAction(action="cancel", run_id=run_id))
    _emit(result)


@app.command("archive")
def archive_cmd(
    run_id: str = typer.Argument(..., help="Curation run ID"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Archive a completed, failed, or canceled curation run."""
    _init_and_user(user_id)
    result = manage_curation_runs(options=CurationRunAction(action="archive", run_id=run_id))
    _emit(result)
