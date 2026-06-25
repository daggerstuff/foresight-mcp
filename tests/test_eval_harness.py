"""Tests for evaluation harness (PIX-3953).

Covers: PII scanner, fixture seeding, scenario execution, report
formatting, metric collection, edge cases, and baseline comparison.
"""

from __future__ import annotations

import json
import os
import tempfile

from foresight_mcp.eval_harness import (
    FIXTURE_MEMORIES,
    SCENARIOS,
    EvalHarness,
    EvalReport,
    ScenarioResult,
    scan_for_pii,
)

# =============================================================================
# PII Scanner
# =============================================================================


class TestPiiScanner:
    def test_detects_email(self):
        findings = scan_for_pii("Contact me at user@example.com for details.")
        assert len(findings) >= 1
        assert findings[0]["type"] == "email"

    def test_detects_phone(self):
        findings = scan_for_pii("Call me at 555-123-4567")
        assert len(findings) >= 1
        assert findings[0]["type"] == "phone"

    def test_detects_ssn(self):
        findings = scan_for_pii("SSN: 123-45-6789")
        assert len(findings) >= 1
        assert findings[0]["type"] == "ssn"

    def test_detects_api_key(self):
        findings = scan_for_pii("api_key = sk-abcdef1234567890abcdef12")
        assert len(findings) >= 1
        assert findings[0]["type"] == "api_key"

    def test_detects_ip_address(self):
        findings = scan_for_pii("Server IP is 192.168.1.1")
        assert len(findings) >= 1
        assert findings[0]["type"] == "ip_address"

    def test_clean_text_no_findings(self):
        findings = scan_for_pii("The user prefers TypeScript with explicit generics.")
        assert findings == []

    def test_empty_text(self):
        assert scan_for_pii("") == []

    def test_matches_are_truncated(self):
        findings = scan_for_pii("email is verylongemailaddress@example.com for testing")
        if findings:
            assert len(findings[0]["match"]) <= 20


# =============================================================================
# Fixture definitions
# =============================================================================


class TestFixtures:
    def test_fixtures_have_required_fields(self):
        for mem in FIXTURE_MEMORIES:
            assert "id" in mem, f"Missing id in fixture: {mem}"
            assert "content" in mem, f"Missing content in fixture: {mem.get('id')}"
            assert "scope" in mem, f"Missing scope in fixture: {mem['id']}"
            assert "category" in mem, f"Missing category in fixture: {mem['id']}"
            assert "importance" in mem, f"Missing importance in fixture: {mem['id']}"

    def test_fixtures_have_unique_ids(self):
        ids = [mem["id"] for mem in FIXTURE_MEMORIES]
        assert len(ids) == len(set(ids)), f"Duplicate IDs found: {ids}"

    def test_all_scenario_expected_ids_exist(self):
        fixture_ids = {mem["id"] for mem in FIXTURE_MEMORIES}
        for scenario in SCENARIOS:
            missing = scenario.expected_memory_ids - fixture_ids
            assert not missing, f"Scenario {scenario.id} expects unknown IDs: {missing}"


# =============================================================================
# Scenario definitions
# =============================================================================


class TestScenarios:
    def test_scenarios_have_required_fields(self):
        for s in SCENARIOS:
            assert s.id, "Scenario missing id"
            assert s.query, f"Scenario {s.id} missing query"
            assert s.expected_memory_ids, f"Scenario {s.id} missing expected_memory_ids"
            assert s.pass_condition, f"Scenario {s.id} missing pass_condition"

    def test_scenario_ids_are_unique(self):
        ids = [s.id for s in SCENARIOS]
        assert len(ids) == len(set(ids)), f"Duplicate scenario IDs: {ids}"


# =============================================================================
# Harness — fixture seeding
# =============================================================================


class TestHarnessSeeding:
    def test_seed_fixtures_creates_memories(self):
        harness = EvalHarness(db_path=":memory:")
        try:
            count = harness.seed_fixtures()
            assert count > 0, "No fixtures were seeded"
            conn = harness._get_connection()
            row = conn.execute("SELECT COUNT(*) as cnt FROM memories").fetchone()
            assert row["cnt"] == len(FIXTURE_MEMORIES), f"Expected {len(FIXTURE_MEMORIES)} memories, got {row['cnt']}"
        finally:
            harness.close()

    def test_seed_fixtures_creates_entities(self):
        harness = EvalHarness(db_path=":memory:")
        try:
            harness.seed_fixtures()
            conn = harness._get_connection()
            row = conn.execute("SELECT COUNT(*) as cnt FROM memory_entities").fetchone()
            assert row["cnt"] >= 5, f"Expected >=5 entities, got {row['cnt']}"
            row = conn.execute("SELECT COUNT(*) as cnt FROM memory_entity_links").fetchone()
            assert row["cnt"] >= 5, f"Expected >=5 links, got {row['cnt']}"
        finally:
            harness.close()

    def test_seed_fixtures_idempotent(self):
        harness = EvalHarness(db_path=":memory:")
        try:
            harness.seed_fixtures()
            harness.seed_fixtures()
            conn = harness._get_connection()
            row = conn.execute("SELECT COUNT(*) as cnt FROM memories").fetchone()
            assert row["cnt"] == len(FIXTURE_MEMORIES)
        finally:
            harness.close()

    def test_custom_user_and_tenant(self):
        harness = EvalHarness(
            db_path=":memory:",
            user_id="custom_user",
            tenant_id="custom_tenant",
        )
        try:
            harness.seed_fixtures()
            conn = harness._get_connection()
            row = conn.execute("SELECT DISTINCT user_id FROM memories").fetchone()
            assert row["user_id"] == "custom_user"
            row = conn.execute("SELECT DISTINCT tenant_id FROM memories").fetchone()
            assert row["tenant_id"] == "custom_tenant"
        finally:
            harness.close()

    def test_file_based_db(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            harness = EvalHarness(db_path=db_path)
            harness.seed_fixtures()
            harness.close()
            assert os.path.getsize(db_path) > 0, "DB file should contain data"
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)


# =============================================================================
# Harness — scenario execution
# =============================================================================


class TestScenarioExecution:
    def test_run_scenario_returns_result(self):
        harness = EvalHarness(db_path=":memory:")
        try:
            harness.seed_fixtures()
            scenario = SCENARIOS[0]
            result = harness.run_scenario(scenario)
            assert isinstance(result, ScenarioResult)
            assert result.scenario_id == scenario.id
            assert result.query == scenario.query
            assert result.injection_payload_size >= 0
            assert result.latency_ms >= 0
            assert isinstance(result.found_memory_ids, list)
            assert isinstance(result.pii_findings, list)
        finally:
            harness.close()

    def test_run_scenario_no_error(self):
        harness = EvalHarness(db_path=":memory:")
        try:
            harness.seed_fixtures()
            for scenario in SCENARIOS:
                result = harness.run_scenario(scenario)
                assert result.error is None, f"Scenario {scenario.id} failed: {result.error}"
        finally:
            harness.close()

    def test_run_all_returns_report(self):
        harness = EvalHarness(db_path=":memory:")
        try:
            harness.seed_fixtures()
            report = harness.run_all()
            assert isinstance(report, EvalReport)
            assert len(report.scenarios) == len(SCENARIOS)
            assert report.summary["total"] == len(SCENARIOS)
        finally:
            harness.close()

    def test_run_all_pii_scanning(self):
        harness = EvalHarness(db_path=":memory:")
        try:
            harness.seed_fixtures()
            report = harness.run_all()
            # The fixture memories should NOT contain PII, so no findings
            total_pii = report.summary["pii_findings_total"]
            assert total_pii == 0, f"Expected 0 PII findings in clean fixtures, got {total_pii}"
        finally:
            harness.close()


# =============================================================================
# Report formatting
# =============================================================================


class TestReportFormatting:
    def test_format_text(self):
        harness = EvalHarness(db_path=":memory:")
        try:
            harness.seed_fixtures()
            report = harness.run_all()
            text = report.format_text()
            assert "FORESIGHT EVALUATION HARNESS" in text
            assert report.summary["total"] > 0
            # Status characters for each scenario
            for sr in report.scenarios:
                status_char = "✓" if sr.passed else "✗"
                assert status_char in text
        finally:
            harness.close()

    def test_to_dict(self):
        harness = EvalHarness(db_path=":memory:")
        try:
            harness.seed_fixtures()
            report = harness.run_all()
            d = report.to_dict()
            assert "timestamp" in d
            assert "summary" in d
            assert "scenarios" in d
            assert d["summary"]["total"] == len(SCENARIOS)
            for sd in d["scenarios"]:
                assert "scenario_id" in sd
                assert "query" in sd
                assert "passed" in sd
                assert "injection_payload_size" in sd
                assert "latency_ms" in sd
        finally:
            harness.close()

    def test_empty_report(self):
        report = EvalReport(
            timestamp="2026-01-01T00:00:00",
            scenarios=[],
            summary={"total": 0, "passed": 0, "pass_rate_pct": 0.0},
        )
        text = report.format_text()
        assert "0/0" in text
        d = report.to_dict()
        assert d["summary"]["total"] == 0


# =============================================================================
# Scenario result
# =============================================================================


class TestScenarioResult:
    def test_creation(self):
        result = ScenarioResult(
            scenario_id="test_scenario",
            query="test query",
            passed=True,
            metrics={"total_candidates": 10},
            found_memory_ids=["mem1", "mem2"],
            missing_expected=[],
            injection_payload_size=500,
            latency_ms=12.34,
            signal_counts={"keyword": 2, "tfidf_cosine": 1},
            fast_path=None,
            pii_findings=[],
        )
        assert result.passed
        assert result.scenario_id == "test_scenario"
        assert result.injection_payload_size == 500


# =============================================================================
# Baseline comparison
# =============================================================================


class TestBaselineComparison:
    def test_compare_baseline(self):
        harness = EvalHarness(db_path=":memory:")
        try:
            harness.seed_fixtures()
            report1 = harness.run_all()
            report2 = harness.run_all()
            diff = harness.compare_baseline(report1, report2)
            assert "payload_change_pct" in diff
            assert "latency_change_pct" in diff
            assert "pass_rate_change" in diff
            assert "scenario_diffs" in diff
            assert len(diff["scenario_diffs"]) == len(SCENARIOS)
        finally:
            harness.close()

    def test_compare_baseline_scenario_status(self):
        harness = EvalHarness(db_path=":memory:")
        try:
            harness.seed_fixtures()
            report1 = harness.run_all()
            report2 = harness.run_all()
            diff = harness.compare_baseline(report1, report2)
            for sd in diff["scenario_diffs"]:
                # If both runs produced same pass/fail, status is "unchanged"
                assert sd["status_change"] == "unchanged", (
                    f"Scenario {sd['scenario_id']} status changed: {sd['status_change']}"
                )
        finally:
            harness.close()


# =============================================================================
# Edge cases
# =============================================================================


class TestEdgeCases:
    def test_harness_cleanup(self):
        """Calling close() should free resources."""
        harness = EvalHarness(db_path=":memory:")
        harness.seed_fixtures()
        harness.close()
        assert harness._conn is None

    def test_run_all_multiple_times(self):
        """Running the harness multiple times should produce consistent results."""
        harness = EvalHarness(db_path=":memory:")
        try:
            harness.seed_fixtures()
            report1 = harness.run_all()
            report2 = harness.run_all()
            # Same fixtures, same queries — pass rates should be identical
            assert report1.summary["pass_rate_pct"] == report2.summary["pass_rate_pct"]
        finally:
            harness.close()

    def test_stale_vs_current_scoring(self):
        """S3: current_auth_approach should rank higher than stale_auth_approach."""
        harness = EvalHarness(db_path=":memory:")
        try:
            harness.seed_fixtures()
            s3 = next(s for s in SCENARIOS if s.id == "S3_stale_vs_current")
            result = harness.run_scenario(s3)
            # Both expected memories should appear
            assert "current_auth_approach" in result.found_memory_ids, (
                f"current_auth_approach not found in {result.found_memory_ids}"
            )
            assert "stale_auth_approach" in result.found_memory_ids, (
                f"stale_auth_approach not found in {result.found_memory_ids}"
            )
            # Current should rank higher (lower index = higher rank)
            idx_current = result.found_memory_ids.index("current_auth_approach")
            idx_stale = result.found_memory_ids.index("stale_auth_approach")
            assert idx_current < idx_stale, "current(idx_current) should rank higher than stale(idx_stale)"
        finally:
            harness.close()


# =============================================================================
# CLI entry point
# =============================================================================


class TestRunEval:
    def test_run_eval_returns_report(self):
        from foresight_mcp.eval_harness import run_eval

        report = run_eval(db_path=":memory:", budget_chars=2000)
        assert isinstance(report, EvalReport)
        assert len(report.scenarios) == len(SCENARIOS)
        assert report.summary["total"] == len(SCENARIOS)
        assert report.summary["pass_rate_pct"] >= 0

    def test_run_eval_writes_report_file(self):
        from foresight_mcp.eval_harness import run_eval

        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            report_path = f.name
        try:
            run_eval(
                db_path=":memory:",
                report_path=report_path,
                budget_chars=2000,
            )
            assert os.path.exists(report_path)
            with open(report_path) as f:
                data = json.load(f)
            assert "summary" in data
            assert "scenarios" in data
            assert data["summary"]["total"] == len(SCENARIOS)
        finally:
            if os.path.exists(report_path):
                os.unlink(report_path)


# =============================================================================
# Report save/load persistence
# =============================================================================


class TestReportPersistence:
    def test_save_and_load(self):
        harness = EvalHarness(db_path=":memory:")
        try:
            harness.seed_fixtures()
            report1 = harness.run_all()
            with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
                path = f.name
            try:
                report1.save(path)
                report2 = EvalReport.load(path)
                assert report2.harness_version == report1.harness_version
                assert len(report2.scenarios) == len(report1.scenarios)
                assert report2.summary["total"] == report1.summary["total"]
                for s1, s2 in zip(report1.scenarios, report2.scenarios, strict=True):
                    assert s1.scenario_id == s2.scenario_id
                    assert s1.passed == s2.passed
                    assert s1.injection_payload_size == s2.injection_payload_size
                    assert s1.latency_ms == s2.latency_ms
            finally:
                if os.path.exists(path):
                    os.unlink(path)
        finally:
            harness.close()

    def test_compare_via_cli(self):
        """run_eval with compare_path should not raise."""
        from foresight_mcp.eval_harness import run_eval

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            baseline_path = f.name
        try:
            # Generate baseline
            run_eval(db_path=":memory:", save_baseline=baseline_path, budget_chars=2000)
            assert os.path.exists(baseline_path)

            # Run comparison against baseline
            report = run_eval(
                db_path=":memory:",
                compare_path=baseline_path,
                budget_chars=2000,
            )
            assert isinstance(report, EvalReport)
            assert report.summary["total"] == len(SCENARIOS)
        finally:
            if os.path.exists(baseline_path):
                os.unlink(baseline_path)

    def test_compare_missing_baseline_does_not_crash(self):
        from foresight_mcp.eval_harness import run_eval

        report = run_eval(
            db_path=":memory:",
            compare_path="/nonexistent/baseline.json",
            budget_chars=2000,
        )
        assert isinstance(report, EvalReport)

    def test_invalid_json_baseline_does_not_crash(self):
        from foresight_mcp.eval_harness import run_eval

        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            f.write("not json")
            bad_path = f.name
        try:
            report = run_eval(db_path=":memory:", compare_path=bad_path, budget_chars=2000)
            assert isinstance(report, EvalReport)
        finally:
            if os.path.exists(bad_path):
                os.unlink(bad_path)
