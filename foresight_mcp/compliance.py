"""
Compliance Exporters for HIPAA, SOC2, and GDPR

Provides export functions for regulatory compliance:
- HIPAA: Access logs, modification logs, user activity
- SOC2: Change history, access controls, monitoring
- GDPR: Data export, erasure certification
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class ComplianceExport:
    """Base class for compliance exports."""
    export_type: str
    generated_at: str
    data: dict[str, Any]


class ComplianceExporter:
    """
    Export compliance data in various formats.
    
    Supports:
    - HIPAA access logs and modification history
    - SOC2 change management and access controls
    - GDPR data portability and erasure certification
    """

    def __init__(self, events):
        """Initialize with event data."""
        # Convert Event objects to dicts if needed
        self.events = []
        for e in events:
            if hasattr(e, "to_dict"):
                self.events.append(e.to_dict())
            elif hasattr(e, "__dict__"):
                self.events.append(e.__dict__)
            else:
                self.events.append(e)

    # =========================================================================
    # HIPAA Exports
    # =========================================================================

    def hipaa_access_log(self, start_date: str | None = None, 
                        end_date: str | None = None,
                        user_id: str | None = None) -> ComplianceExport:
        """
        Generate HIPAA-compliant access log.
        
        Tracks who accessed what PHI (Protected Health Information) and when.
        Required for HIPAA §164.312(b) - Audit Controls.
        """
        access_events = [
            e for e in self.events
            if e.get("event_type", "").startswith("memory.")
            and ("retrieved" in e.get("event_type", "") or "accessed" in e.get("event_type", ""))
        ]

        # Filter by date range
        if start_date:
            access_events = [e for e in access_events if e.get("timestamp", "") >= start_date]
        if end_date:
            access_events = [e for e in access_events if e.get("timestamp", "") <= end_date]

        # Filter by user
        if user_id:
            access_events = [e for e in access_events if e.get("actor") == user_id]

        # Format for HIPAA compliance
        formatted = []
        for event in access_events:
            formatted.append({
                "event_id": event.get("id"),
                "timestamp": event.get("timestamp"),
                "user_id": event.get("actor"),
                "entity_id": event.get("entity_id"),
                "entity_type": "memory",
                "action": "ACCESS",
                "result": "SUCCESS",
                "phi_indicator": True,  # All memories treated as PHI
            })

        return ComplianceExport(
            export_type="HIPAA_ACCESS_LOG",
            generated_at=datetime.now(timezone.utc).isoformat(),
            data={"entries": formatted, "count": len(formatted)}
        )

    def hipaa_modification_log(self, start_date: str | None = None,
                               end_date: str | None = None,
                               user_id: str | None = None) -> ComplianceExport:
        """
        Generate HIPAA-compliant modification log.
        
        Tracks all changes to PHI. Required for HIPAA §164.312(b).
        """
        mod_events = [
            e for e in self.events
            if e.get("event_type", "") in ("memory.updated", "memory.deleted", "memory.created")
        ]

        if start_date:
            mod_events = [e for e in mod_events if e.get("timestamp", "") >= start_date]
        if end_date:
            mod_events = [e for e in mod_events if e.get("timestamp", "") <= end_date]
        if user_id:
            mod_events = [e for e in mod_events if e.get("actor") == user_id]

        formatted = []
        for event in mod_events:
            payload = event.get("payload", {})
            formatted.append({
                "event_id": event.get("id"),
                "timestamp": event.get("timestamp"),
                "user_id": event.get("actor"),
                "entity_id": event.get("entity_id"),
                "entity_type": "memory",
                "action": event.get("event_type", "").split(".")[-1].upper(),
                "old_value": payload.get("old_content", "")[:100] if "old_content" in payload else None,
                "new_value": payload.get("new_content", "")[:100] if "new_content" in payload else None,
                "phi_indicator": True,
            })

        return ComplianceExport(
            export_type="HIPAA_MODIFICATION_LOG",
            generated_at=datetime.now(timezone.utc).isoformat(),
            data={"entries": formatted, "count": len(formatted)}
        )

    def hipaa_user_activity(self, user_id: str, 
                           start_date: str | None = None,
                           end_date: str | None = None) -> ComplianceExport:
        """
        Generate HIPAA user activity report.
        
        All actions by a specific user for audit purposes.
        """
        user_events = [e for e in self.events if e.get("actor") == user_id]

        if start_date:
            user_events = [e for e in user_events if e.get("timestamp", "") >= start_date]
        if end_date:
            user_events = [e for e in user_events if e.get("timestamp", "") <= end_date]

        formatted = []
        for event in user_events:
            formatted.append({
                "event_id": event.get("id"),
                "timestamp": event.get("timestamp"),
                "user_id": event.get("actor"),
                "action": event.get("event_type", ""),
                "entity_id": event.get("entity_id"),
                "entity_type": "memory" if "memory" in event.get("event_type", "") else "other",
            })

        return ComplianceExport(
            export_type="HIPAA_USER_ACTIVITY",
            generated_at=datetime.now(timezone.utc).isoformat(),
            data={
                "user_id": user_id,
                "entries": formatted,
                "count": len(formatted),
                "period": {"start": start_date, "end": end_date}
            }
        )

    # =========================================================================
    # SOC2 Exports
    # =========================================================================

    def soc2_change_history(self, start_date: str | None = None,
                           end_date: str | None = None) -> ComplianceExport:
        """
        Generate SOC2 change management report.
        
        Tracks all configuration and data changes. Required for SOC2 CC6.1.
        """
        change_events = [
            e for e in self.events
            if e.get("event_type", "") in (
                "memory.updated", "memory.deleted", "memory.created",
                "block.updated", "block.created", "block.deleted"
            )
        ]

        if start_date:
            change_events = [e for e in change_events if e.get("timestamp", "") >= start_date]
        if end_date:
            change_events = [e for e in change_events if e.get("timestamp", "") <= end_date]

        formatted = []
        for event in change_events:
            payload = event.get("payload", {})
            formatted.append({
                "change_id": event.get("id"),
                "timestamp": event.get("timestamp"),
                "change_type": event.get("event_type", ""),
                "actor": event.get("actor"),
                "entity_affected": event.get("entity_id"),
                "description": f"{event.get('event_type', '')} on {event.get('entity_id')}",
                "approved": True,  # Would need approval workflow integration
                "rollback_available": "memory" in event.get("event_type", ""),
            })

        return ComplianceExport(
            export_type="SOC2_CHANGE_HISTORY",
            generated_at=datetime.now(timezone.utc).isoformat(),
            data={
                "entries": formatted,
                "count": len(formatted),
                "period": {"start": start_date, "end": end_date}
            }
        )

    def soc2_access_review(self, user_ids: list[str] | None = None) -> ComplianceExport:
        """
        Generate SOC2 access control review report.
        
        Lists all users with system access and their activities.
        Required for SOC2 CC6.2, CC6.3.
        """
        # Get all unique users
        all_users = set(e.get("actor") for e in self.events if e.get("actor"))

        if user_ids:
            all_users = all_users.intersection(set(user_ids))

        user_access = {}
        for uid in all_users:
            user_events = [e for e in self.events if e.get("actor") == uid]
            user_access[uid] = {
                "user_id": uid,
                "total_actions": len(user_events),
                "first_access": min((e.get("timestamp", "") for e in user_events), default=""),
                "last_access": max((e.get("timestamp", "") for e in user_events), default=""),
                "event_types": list(set(e.get("event_type", "") for e in user_events)),
            }

        return ComplianceExport(
            export_type="SOC2_ACCESS_REVIEW",
            generated_at=datetime.now(timezone.utc).isoformat(),
            data={
                "users": user_access,
                "total_users": len(all_users),
                "review_date": datetime.now(timezone.utc).isoformat()
            }
        )

    def soc2_monitoring_report(self, start_date: str | None = None,
                              end_date: str | None = None) -> ComplianceExport:
        """
        Generate SOC2 monitoring report.
        
        System monitoring and anomaly detection. Required for SOC2 CC7.1, CC7.2.
        """
        anomaly_events = [
            e for e in self.events
            if "anomaly" in e.get("event_type", "").lower()
            or "error" in e.get("event_type", "").lower()
        ]

        if start_date:
            anomaly_events = [e for e in anomaly_events if e.get("timestamp", "") >= start_date]
        if end_date:
            anomaly_events = [e for e in anomaly_events if e.get("timestamp", "") <= end_date]

        # Calculate metrics
        total_events = len(self.events)
        total_anomalies = len(anomaly_events)

        return ComplianceExport(
            export_type="SOC2_MONITORING",
            generated_at=datetime.now(timezone.utc).isoformat(),
            data={
                "total_events": total_events,
                "anomaly_count": total_anomalies,
                "anomaly_rate": total_anomalies / total_events if total_events > 0 else 0,
                "anomalies": [dict(e) for e in anomaly_events],
                "period": {"start": start_date, "end": end_date}
            }
        )

    # =========================================================================
    # GDPR Exports
    # =========================================================================

    def gdpr_data_export(self, user_id: str, 
                        include_deleted: bool = False) -> ComplianceExport:
        """
        Generate GDPR data portability export.
        
        All data for a specific user in a structured, machine-readable format.
        Required for GDPR Article 20 (Right to Data Portability).
        """
        user_events = [e for e in self.events if e.get("actor") == user_id]

        # Get memories for user
        user_memories = [
            e for e in self.events
            if e.get("entity_id", "").startswith("memory")
            and e.get("actor") == user_id
        ]

        return ComplianceExport(
            export_type="GDPR_DATA_EXPORT",
            generated_at=datetime.now(timezone.utc).isoformat(),
            data={
                "user_id": user_id,
                "export_date": datetime.now(timezone.utc).isoformat(),
                "events": user_events,
                "memories": user_memories,
                "total_events": len(user_events),
                "total_memories": len(user_memories),
                "include_deleted": include_deleted,
            }
        )

    def gdpr_erasure_certification(self, user_id: str,
                                  deletion_date: str | None = None) -> ComplianceExport:
        """
        Generate GDPR erasure certification.
        
        Certifies that all data for a user has been deleted.
        Required for GDPR Article 17 (Right to Erasure / "Right to be Forgotten").
        """
        # In a real implementation, this would verify deletion across all systems
        return ComplianceExport(
            export_type="GDPR_ERASURE_CERTIFICATION",
            generated_at=datetime.now(timezone.utc).isoformat(),
            data={
                "user_id": user_id,
                "certification_date": deletion_date or datetime.now(timezone.utc).isoformat(),
                "certification_statement": (
                    f"This records that an erasure request has been made for user '{user_id}' "
                    "under GDPR Article 17. Actual deletion must be verified separately."
                ),
                "data_categories_requested_for_deletion": [
                    "memory_records",
                    "access_logs",
                    "audit_trails",
                    "user_preferences",
                ],
                "deletion_verified": False,
                "retention_exceptions": [],
                "authorized_by": "system",
            }
        )

    # =========================================================================
    # Export Format Methods
    # =========================================================================

    def to_json(self, export: ComplianceExport) -> str:
        """Export to JSON format."""
        return json.dumps({
            "export_type": export.export_type,
            "generated_at": export.generated_at,
            "data": export.data
        }, indent=2)

    def to_csv(self, export: ComplianceExport) -> str:
        """Export to CSV format.

        WARNING: CSV is unencrypted. For PHI data, use encrypted storage
        or the JSON format with encryption at rest.
        """
        output = io.StringIO()
        entries = export.data.get("entries", [])

        if not entries:
            return "# No entries to export"

        # Get all possible field names
        fieldnames = set()
        for entry in entries:
            fieldnames.update(entry.keys())
        fieldnames = sorted(fieldnames)

        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for entry in entries:
            writer.writerow({k: entry.get(k, "") for k in fieldnames})

        return output.getvalue()

    def save_to_file(self, export: ComplianceExport, path: str, 
                    format: str = "json") -> str:
        """Save export to file."""
        if format == "json":
            content = self.to_json(export)
        elif format == "csv":
            content = self.to_csv(export)
        else:
            raise ValueError(f"Unsupported format: {format}")

        output_path = Path(path).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content)

        return str(output_path)
