"""
Audit Trail Projection Reports
Five compliance-focused projections for audit and reporting
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .base import BaseProjection


@dataclass
class MemoryTimeline(BaseProjection):
    """
    Chronological view of all memory operations.

    Shows complete history of memory lifecycle events:
    - memory.stored
    - memory.retrieved
    - memory.updated
    - memory.deleted
    """

    name: str = "Memory Timeline"
    description: str = "Chronological view of all memory operations"

    def build(self, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Build timeline from events."""
        timeline = []

        for event in events:
            event_type = event.get("event_type", "")
            if not event_type.startswith("memory."):
                continue

            timeline.append({
                "event_id": event.get("id"),
                "event_type": event_type,
                "timestamp": event.get("timestamp"),
                "actor": event.get("actor"),
                "entity_id": event.get("entity_id"),
                "content_preview": self._truncate(event.get("payload", {}).get("content", "")),
                "metadata": event.get("metadata", {}),
            })

        # Sort by timestamp
        timeline.sort(key=lambda x: x.get("timestamp", ""))
        return timeline

    def _truncate(self, text: str, max_len: int = 50) -> str:
        if len(text) > max_len:
            return text[:max_len] + "..."
        return text

    def to_csv(self, data: List[Dict[str, Any]]) -> str:
        """Convert timeline to CSV."""
        output = io.StringIO()
        fieldnames = ["event_id", "event_type", "timestamp", "actor", "entity_id", "content_preview"]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in data:
            writer.writerow({k: row.get(k, "") for k in fieldnames})
        return output.getvalue()


@dataclass
class UserActivityReport(BaseProjection):
    """
    Per-user memory operations report.

    Groups memory operations by user for activity tracking.
    """

    name: str = "User Activity Report"
    description: str = "Per-user memory operations"

    def build(self, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Build user activity report."""
        by_user: Dict[str, List[Dict[str, Any]]] = {}

        for event in events:
            if not event.get("event_type", "").startswith("memory."):
                continue

            actor = event.get("actor", "unknown")
            if actor not in by_user:
                by_user[actor] = []

            by_user[actor].append({
                "event_id": event.get("id"),
                "event_type": event.get("event_type"),
                "timestamp": event.get("timestamp"),
                "entity_id": event.get("entity_id"),
            })

        # Convert to report format
        report = []
        for user_id, user_events in by_user.items():
            user_events.sort(key=lambda x: x.get("timestamp", ""))

            report.append({
                "user_id": user_id,
                "total_events": len(user_events),
                "events": user_events,
                "first_activity": user_events[0].get("timestamp") if user_events else None,
                "last_activity": user_events[-1].get("timestamp") if user_events else None,
            })

        report.sort(key=lambda x: x["total_events"], reverse=True)
        return report

    def to_csv(self, data: List[Dict[str, Any]]) -> str:
        """Convert to CSV."""
        output = io.StringIO()
        fieldnames = ["user_id", "total_events", "first_activity", "last_activity"]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in data:
            writer.writerow({
                "user_id": row["user_id"],
                "total_events": row["total_events"],
                "first_activity": row["first_activity"],
                "last_activity": row["last_activity"],
            })
        return output.getvalue()


@dataclass
class BlockChangeLog(BaseProjection):
    """
    All changes to memory blocks.

    Tracks block lifecycle:
    - block.created
    - block.updated
    - block.deleted
    """

    name: str = "Block Change Log"
    description: str = "All changes to memory blocks"

    def build(self, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Build block change log."""
        changes = []

        for event in events:
            event_type = event.get("event_type", "")
            if not event_type.startswith("block."):
                continue

            changes.append({
                "event_id": event.get("id"),
                "event_type": event_type,
                "timestamp": event.get("timestamp"),
                "actor": event.get("actor"),
                "block_label": event.get("entity_id"),
                "old_content": event.get("payload", {}).get("old_content", ""),
                "new_content": event.get("payload", {}).get("new_content", ""),
            })

        changes.sort(key=lambda x: x.get("timestamp", ""))
        return changes

    def to_csv(self, data: List[Dict[str, Any]]) -> str:
        """Convert to CSV."""
        output = io.StringIO()
        fieldnames = ["event_id", "event_type", "timestamp", "actor", "block_label", "change_summary"]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in data:
            change_summary = f"{row.get('old_content', '')[:20]} -> {row.get('new_content', '')[:20]}"
            writer.writerow({
                "event_id": row.get("event_id", ""),
                "event_type": row.get("event_type", ""),
                "timestamp": row.get("timestamp", ""),
                "actor": row.get("actor", ""),
                "block_label": row.get("block_label", ""),
                "change_summary": change_summary,
            })
        return output.getvalue()


@dataclass
class AccessLog(BaseProjection):
    """
    Who accessed what memory when.

    Tracks memory.retrieved events for access auditing.
    """

    name: str = "Access Log"
    description: str = "Memory access audit trail"

    def build(self, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Build access log."""
        accesses = []

        for event in events:
            if event.get("event_type") != "memory.retrieved":
                continue

            accesses.append({
                "event_id": event.get("id"),
                "timestamp": event.get("timestamp"),
                "actor": event.get("actor"),
                "memory_id": event.get("entity_id"),
                "query_context": event.get("payload", {}).get("query_context", ""),
            })

        accesses.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return accesses

    def to_csv(self, data: List[Dict[str, Any]]) -> str:
        """Convert to CSV."""
        output = io.StringIO()
        fieldnames = ["event_id", "timestamp", "actor", "memory_id", "query_context"]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in data:
            writer.writerow(row)
        return output.getvalue()


@dataclass
class AnomalyReport(BaseProjection):
    """
    Detected anomalies and actions taken.

    Tracks anomaly.detected events for compliance review.
    """

    name: str = "Anomaly Report"
    description: str = "Detected anomalies and actions"

    def build(self, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Build anomaly report."""
        anomalies = []

        for event in events:
            if event.get("event_type") != "anomaly.detected":
                continue

            payload = event.get("payload", {})
            anomalies.append({
                "event_id": event.get("id"),
                "timestamp": event.get("timestamp"),
                "actor": event.get("actor"),
                "category": payload.get("category", ""),
                "risk_level": payload.get("risk_level", ""),
                "entity_id": event.get("entity_id"),
                "details": payload,
            })

        anomalies.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return anomalies

    def to_csv(self, data: List[Dict[str, Any]]) -> str:
        """Convert to CSV."""
        output = io.StringIO()
        fieldnames = ["event_id", "timestamp", "actor", "category", "risk_level", "entity_id"]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in data:
            writer.writerow(row)
        return output.getvalue()
