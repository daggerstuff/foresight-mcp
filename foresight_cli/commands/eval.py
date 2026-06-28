"""Eval command: run the evaluation harness and generate a report."""

from __future__ import annotations

import typer

from ..utils import output as out

app = typer.Typer(help="Run the evaluation harness (PIX-3953).")


@app.command()
def run(
    db_path: str | None = typer.Option(None, "--db-path", help="Path to temp database (default: auto tempfile)"),
    report: str | None = typer.Option(None, "--report", "-r", help="Write JSON report to file"),
    budget: int = typer.Option(2000, "--budget", "-b", help="Character budget for injection payloads"),
    compare: str | None = typer.Option(None, "--compare", "-c", help="Path to a baseline JSON report to diff against"),
    save_baseline: str | None = typer.Option(None, "--save-baseline", help="Save the report as a baseline JSON at this path"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output report as JSON"),
    compare: str | None = typer.Option(None, "--compare", help="Path to a baseline JSON report to diff against"),
    save_baseline: str | None = typer.Option(None, "--save-baseline", help="Write this run as a baseline JSON report"),
):
    """Run the full evaluation harness and print a summary report.

    Seeds fixture memories, runs all 5 evaluation scenarios against
    inject_context and get_relevant_memories, then prints a detailed
    report with metrics on payload size, latency, retrieval quality,
    and PII safety.
    """
    from foresight_mcp.eval_harness import run_eval

    report_obj = run_eval(
        db_path=db_path,
        report_path=report,
        budget_chars=budget,
        compare_path=compare,
        save_baseline=save_baseline,
        json_output=json_output or out.get_settings().mode == "json",
        compare_path=compare,
        save_baseline=save_baseline,
    )

    passed = report_obj.summary["passed"]
    total = report_obj.summary["total"]
    pct = report_obj.summary["pass_rate_pct"]

    if out.get_settings().mode == "json":
        out.print_json(report_obj.to_dict())
    elif out.get_settings().mode == "agent":
        out.data(
            "maintenance_eval_result",
            {
                "passed": passed,
                "total": total,
                "pass_rate_pct": pct,
            },
        )
    else:
        out.done(f"{passed}/{total} scenarios passed ({pct:.1f}%)")
        for sr in report_obj.scenarios:
            status = "✓" if sr.passed else "✗"
            icon = "green" if sr.passed else "red"
            payload = f"payload={sr.injection_payload_size} chars"
            latency = f"latency={sr.latency_ms:.1f}ms"
            findings = f"pii={len(sr.pii_findings)}"
            out.stderr(
                f"  [{icon}]{status}[/] {sr.scenario_id}: {payload}, {latency}, {findings}",
                style=icon,
            )
        if report:
            out.info(f"Maintenance eval report written to {report}")
        if save_baseline:
            out.info(f"Baseline report written to {save_baseline}")
