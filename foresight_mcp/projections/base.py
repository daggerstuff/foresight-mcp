"""
Base projection class for audit trail reports
"""
from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class BaseProjection(ABC):
    """
    Abstract base class for all projections.

    Projections are materialized views built from event data.
    Each projection serves a specific compliance use case.
    """

    name: str
    description: str

    @abstractmethod
    def build(self, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Build projection from events."""
        pass

    @abstractmethod
    def to_csv(self, data: List[Dict[str, Any]]) -> str:
        """Convert projection data to CSV."""
        pass

    def to_json(self, data: List[Dict[str, Any]], indent: int = 2) -> str:
        """Convert projection data to JSON."""
        return json.dumps(data, indent=indent)

    def filter_by_date(
        self,
        data: List[Dict[str, Any]],
        start: Optional[datetime] = None,
        end: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """Filter data by date range."""
        if not start and not end:
            return data

        result = []
        for item in data:
            ts = item.get("timestamp")
            if ts:
                item_date = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if start and item_date < start:
                    continue
                if end and item_date > end:
                    continue
            result.append(item)

        return result

    def filter_by_user(
        self,
        data: List[Dict[str, Any]],
        user_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Filter data by user ID."""
        if not user_id:
            return data

        return [item for item in data if item.get("user_id") == user_id]
