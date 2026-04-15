"""
Projection Builder - Builds and materializes audit trail projections
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import BaseProjection
from .reports import (
    MemoryTimeline,
    UserActivityReport,
    BlockChangeLog,
    AccessLog,
    AnomalyReport,
)


class ProjectionBuilder:
    """
    Builds and manages audit trail projections.

    Projections are materialized views built from the event store.
    Each projection serves a specific compliance use case.
    """

    def __init__(self, db_path: Optional[str] = None):
        """Initialize projection builder.

        Args:
            db_path: Path to SQLite database (default: ~/.foresight/projections.db)
        """
        if db_path is None:
            db_path = str(Path.home() / ".foresight" / "projections.db")

        self.db_path = db_path
        self._init_db()

        # Initialize reports
        self._reports = {
            "memory_timeline": MemoryTimeline(),
            "user_activity": UserActivityReport(),
            "block_changes": BlockChangeLog(),
            "access_log": AccessLog(),
            "anomaly_report": AnomalyReport(),
        }

    def _init_db(self) -> None:
        """Initialize database schema."""
        db_path = Path(self.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS projections (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                data TEXT NOT NULL,
                built_at TEXT NOT NULL,
                start_date TEXT,
                end_date TEXT,
                user_filter TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_projections_name ON projections(name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_projections_built ON projections(built_at)")
        conn.commit()
        conn.close()

    def build_all(
        self,
        events: List[Dict[str, Any]],
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        user_filter: Optional[str] = None
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Build all projections from events.

        Args:
            events: List of events from event store
            start_date: Optional start date filter
            end_date: Optional end date filter
            user_filter: Optional user ID filter

        Returns:
            Dictionary of projection name to data
        """
        results = {}

        for name, report in self._reports.items():
            # Build projection
            data = report.build(events)

            # Apply filters
            if start_date or end_date:
                data = report.filter_by_date(data, start_date, end_date)
            if user_filter:
                data = report.filter_by_user(data, user_filter)

            results[name] = data

        return results

    def get_report(self, name: str) -> Optional[BaseProjection]:
        """Get a report by name."""
        return self._reports.get(name)

    def export_csv(
        self,
        name: str,
        events: List[Dict[str, Any]],
        output_path: str
    ) -> str:
        """Build and export a projection to CSV.

        Args:
            name: Report name (memory_timeline, user_activity, etc.)
            events: List of events
            output_path: Path to write CSV

        Returns:
            Path to generated CSV
        """
        report = self._reports.get(name)
        if not report:
            raise ValueError(f"Unknown report: {name}")

        data = report.build(events)
        csv_content = report.to_csv(data)

        with open(output_path, "w") as f:
            f.write(csv_content)

        return output_path

    def export_json(
        self,
        name: str,
        events: List[Dict[str, Any]],
        output_path: str,
        indent: int = 2
    ) -> str:
        """Build and export a projection to JSON.

        Args:
            name: Report name
            events: List of events
            output_path: Path to write JSON
            indent: JSON indentation

        Returns:
            Path to generated JSON
        """
        report = self._reports.get(name)
        if not report:
            raise ValueError(f"Unknown report: {name}")

        data = report.build(events)
        json_content = report.to_json(data, indent)

        with open(output_path, "w") as f:
            f.write(json_content)

        return output_path

    def list_reports(self) -> List[str]:
        """List available report names."""
        return list(self._reports.keys())

    def get_report_summary(self, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Get summary statistics for all reports.

        Args:
            events: List of events

        Returns:
            Summary dictionary
        """
        summary = {}

        for name, report in self._reports.items():
            data = report.build(events)
            summary[name] = {
                "record_count": len(data),
                "name": report.name,
                "description": report.description,
            }

        return summary
