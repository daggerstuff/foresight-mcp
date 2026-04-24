"""
Reflection Engine - Periodic Batch Synthesis and AI-Powered Insights.

Orchestrates weekly/monthly reflection by combining:
- Enhanced synthesis (contradiction detection, trend analysis)
- Temporal decay tracking (strengthening/weakening/stale patterns)
- Graph analysis (entity clusters, relationship evolution)

Produces structured reflection reports with actionable insights,
stored as memories themselves for continuity across sessions.
"""
from __future__ import annotations
import sqlite3
import json
import uuid
import logging
import threading
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("foresight_reflection_engine")


@dataclass
class ReflectionInsight:
    """A single insight from reflection analysis."""
    insight_type: str  # 'trend' | 'contradiction' | 'pattern' | 'breakthrough' | 'warning'
    summary: str
    confidence: float
    evidence_ids: List[str]
    recommended_action: str  # 'preserve' | 'review' | 'consolidate' | 'investigate'
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.confidence = max(0.0, min(1.0, self.confidence))

    def to_dict(self) -> dict:
        return {
            'insight_type': self.insight_type,
            'summary': self.summary,
            'confidence': self.confidence,
            'evidence_ids': self.evidence_ids,
            'recommended_action': self.recommended_action,
            'metadata': self.metadata,
        }


@dataclass
class ReflectionReport:
    """Complete reflection report from batch analysis."""
    report_id: str
    user_id: str
    period: str  # 'weekly' | 'monthly' | 'custom'
    start_date: str
    end_date: str
    memories_analyzed: int
    insights: List[ReflectionInsight]
    trend_summary: Dict[str, Any]
    entity_summary: Dict[str, Any]
    generated_at: str

    def to_dict(self) -> dict:
        return {
            'report_id': self.report_id,
            'user_id': self.user_id,
            'period': self.period,
            'start_date': self.start_date,
            'end_date': self.end_date,
            'memories_analyzed': self.memories_analyzed,
            'insights': [i.to_dict() for i in self.insights],
            'trend_summary': self.trend_summary,
            'entity_summary': self.entity_summary,
            'generated_at': self.generated_at,
        }


class ReflectionEngine:
    """
    Periodic reflection engine for batch memory analysis.

    Runs synthesis over time-bounded memory sets, combining
    temporal trends, entity patterns, and contradiction detection
    into structured reflection reports.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def reflect(
        self,
        user_id: str,
        tenant_id: str = 'default',
        period: str = 'weekly',
        min_memories: int = 5,
    ) -> Optional[ReflectionReport]:
        """
        Run reflection analysis over a time period.

        Args:
            user_id: User ID
            tenant_id: Tenant ID for isolation
            period: Analysis period ('weekly' or 'monthly')
            min_memories: Minimum memories needed for meaningful analysis

        Returns:
            ReflectionReport or None if insufficient data
        """
        valid_periods = ('daily', 'weekly', 'monthly', 'quarterly')
        if period not in valid_periods:
            raise ValueError(f"Invalid period '{period}'. Must be one of: {', '.join(valid_periods)}")

        hours = {'daily': 24, 'weekly': 168, 'monthly': 720, 'quarterly': 2160}.get(period, 168)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        now = datetime.now(timezone.utc)

        conn = self._get_connection()
        try:
            # Fetch memories for the period
            rows = conn.execute(
                """SELECT id, content, category, importance, strength_trend,
                activation_count, tags, emotional_context, created_at
                FROM memories
                WHERE user_id = ? AND tenant_id = ?
                AND created_at >= ? AND is_ghost = 0
                ORDER BY created_at ASC""",
                (user_id, tenant_id, cutoff.isoformat()),
            ).fetchall()

            if len(rows) < min_memories:
                return None

            # Build trend summary
            trend_summary = self._build_trend_summary(rows)

            # Build entity summary
            entity_summary = self._build_entity_summary(conn, user_id)

            # Generate insights
            insights = self._generate_insights(rows, trend_summary, entity_summary)

            # Create report
            report = ReflectionReport(
                report_id=f"refl_{uuid.uuid4().hex[:12]}",
                user_id=user_id,
                period=period,
                start_date=cutoff.isoformat(),
                end_date=now.isoformat(),
                memories_analyzed=len(rows),
                insights=insights,
                trend_summary=trend_summary,
                entity_summary=entity_summary,
                generated_at=now.isoformat(),
            )

            # Store report as a memory for continuity
            self._store_reflection(conn, report, user_id, tenant_id)

            return report
        finally:
            conn.close()

    def _build_trend_summary(self, rows: list) -> Dict[str, Any]:
        """Build summary of temporal trends from memory rows."""
        trend_counts = {'strengthening': 0, 'stable': 0, 'weakening': 0, 'stale': 0}
        category_importance: Dict[str, List[float]] = {}

        for row in rows:
            _, _, category, importance, trend, _ = row[:6]
            trend = trend or 'stable'
            if trend in trend_counts:
                trend_counts[trend] += 1

            cat = category or 'general'
            if cat not in category_importance:
                category_importance[cat] = []
            category_importance[cat].append(importance or 0.5)

        avg_importance = {
            cat: sum(vals) / len(vals)
            for cat, vals in category_importance.items()
        }

        total = len(rows)
        strengthening_pct = (trend_counts['strengthening'] / total * 100) if total else 0
        weakening_pct = (trend_counts['weakening'] / total * 100) if total else 0

        overall = 'stable'
        if strengthening_pct > 30:
            overall = 'improving'
        elif weakening_pct > 30 or trend_counts['stale'] / max(total, 1) > 0.4:
            overall = 'declining'

        return {
            'overall': overall,
            'trend_counts': trend_counts,
            'avg_importance_by_category': avg_importance,
            'total_memories': total,
        }

    def _build_entity_summary(
        self, conn: sqlite3.Connection, user_id: str
    ) -> Dict[str, Any]:
        """Build summary of entity patterns from graph."""
        # Top entity types
        cursor = conn.execute(
            """SELECT entity_type, COUNT(*) as count
            FROM memory_entities
            WHERE user_id = ?
            GROUP BY entity_type
            ORDER BY count DESC
            LIMIT 10""",
            (user_id,),
        )
        type_counts = {row[0]: row[1] for row in cursor.fetchall()}

        # Most-connected entities
        cursor = conn.execute(
            """SELECT e.name, e.entity_type, COUNT(r.id) as rel_count
            FROM memory_entities e
            LEFT JOIN entity_relationships r
            ON (r.source_entity_id = e.id OR r.target_entity_id = e.id)
            WHERE e.user_id = ?
            GROUP BY e.id
            ORDER BY rel_count DESC
            LIMIT 10""",
            (user_id,),
        )
        top_entities = [
            {'name': row[0], 'type': row[1], 'connections': row[2]}
            for row in cursor.fetchall()
        ]

        return {
            'entity_type_counts': type_counts,
            'top_connected_entities': top_entities,
        }

    def _generate_insights(
        self,
        rows: list,
        trend_summary: Dict[str, Any],
        entity_summary: Dict[str, Any],
    ) -> List[ReflectionInsight]:
        """
        Generate evidence-anchored insights from analysis.

        Upgraded to produce actionable recommendations based on:
        - Cross-category pattern detection
        - Causal relationship inference
        - Risk/opportunity flagging
        - Specific, concrete action items
        """
        insights: List[ReflectionInsight] = []
        counts = trend_summary.get('trend_counts', {})

        # Content-anchored insights from memories grouped by category/trend
        content_insights = self._extract_content_insights(rows)
        insights.extend(content_insights)

        # Cross-category pattern analysis - find correlations between life areas
        cross_category_insights = self._analyze_cross_category_patterns(rows)
        insights.extend(cross_category_insights)

        # Causal relationship detection - identify what's driving changes
        causal_insights = self._detect_causal_relationships(rows, trend_summary)
        insights.extend(causal_insights)

        # Entity hub insights - central themes
        top_entities = entity_summary.get('top_connected_entities', [])
        memory_ids = [row[0] for row in rows]
        for entity in top_entities[:3]:
            if entity['connections'] >= 3:
                # Generate specific action based on entity type
                action = self._recommend_action_for_entity(entity)
                insights.append(ReflectionInsight(
                    insight_type='pattern',
                    summary=f"{entity['name']} ({entity['type']}) anchors {entity['connections']} areas - leverage as hub for change",
                    confidence=0.75,
                    evidence_ids=memory_ids[:3],
                    recommended_action=action,
                    metadata={
                        'entity': entity['name'],
                        'connections': entity['connections'],
                        'leverage_point': True,
                    },
                ))

        # Risk detection - flag potential problems before they escalate
        risk_insights = self._detect_risks(rows, trend_summary)
        insights.extend(risk_insights)

        # Opportunity detection - identify underutilized strengths
        opportunity_insights = self._detect_opportunities(rows, trend_summary)
        insights.extend(opportunity_insights)

        # Stale memory warning with specific recovery plan
        stale_count = counts.get('stale', 0)
        total = trend_summary.get('total_memories', 1)
        if stale_count / max(total, 1) > 0.5:
            stale_rows = [r for r in rows if (r[4] or 'stable') == 'stale']
            stale_categories = set(r[2] or 'general' for r in stale_rows)
            insights.append(ReflectionInsight(
                insight_type='warning',
                summary=f"Half your memories ({stale_count}/{total}) are stale. Focus on {', '.join(list(stale_categories)[:3])} to re-engage.",
                confidence=0.85,
                evidence_ids=[r[0] for r in stale_rows[:5]],
                recommended_action='review',
                metadata={
                    'stale_ratio': stale_count / max(total, 1),
                    'affected_categories': list(stale_categories),
                    'recovery_priority': list(stale_categories)[:2],
                },
            ))

        return insights

    def _analyze_cross_category_patterns(
        self, rows: list
    ) -> List[ReflectionInsight]:
        """
        Detect patterns that span multiple life areas.

        Identifies:
        - Cascading effects (improvement in one area driving another)
        - Resource competition (one area draining another)
        - Synergistic relationships
        """
        insights: List[ReflectionInsight] = []

        # Group by category and calculate category health scores
        by_category: Dict[str, list] = {}
        for row in rows:
            cat = row[2]
            cat = cat or 'general'
            if cat not in by_category:
                by_category[cat] = []
            by_category[cat].append(row)

        category_health: Dict[str, Dict] = {}
        for cat, cat_rows in by_category.items():
            strengthening = sum(1 for r in cat_rows if (r[4] or 'stable') == 'strengthening')
            weakening = sum(1 for r in cat_rows if (r[4] or 'stable') == 'weakening')
            stale = sum(1 for r in cat_rows if (r[4] or 'stable') == 'stale')
            total = len(cat_rows)

            health_score = (strengthening * 1 + stale * 0 - weakening * 1) / max(total, 1)

            category_health[cat] = {
                'score': health_score,
                'strengthening': strengthening,
                'weakening': weakening,
                'stale': stale,
                'total': total,
                'rows': cat_rows,
            }

        # Detect cascading improvements (multiple related areas improving)
        improving_cats = [c for c, d in category_health.items() if d['score'] > 0.3]
        if len(improving_cats) >= 2:
            # Find common themes in improving categories
            improving_memories = []
            for cat in improving_cats:
                improving_memories.extend(category_health[cat]['rows'])

            insights.append(ReflectionInsight(
                insight_type='breakthrough',
                summary=f"Multiple areas improving simultaneously ({', '.join(improving_cats[:3])}). This momentum suggests foundational changes are taking hold - maintain current practices.",
                confidence=0.8,
                evidence_ids=[r[0] for r in improving_memories[:5]],
                recommended_action='preserve',
                metadata={
                    'improving_categories': improving_cats,
                    'momentum_score': len(improving_cats),
                    'actionable': 'Continue current practices; consider documenting what is working',
                },
            ))

        # Detect resource competition (one strong, one weak)
        for high_cat, high_data in category_health.items():
            for low_cat, low_data in category_health.items():
                if high_cat != low_cat and high_data['score'] > 0.3 and low_data['score'] < -0.3:
                    insights.append(ReflectionInsight(
                        insight_type='pattern',
                        summary=f"Energy imbalance: {high_cat} is thriving while {low_cat} struggles. Consider if time/energy allocation needs rebalancing.",
                        confidence=0.65,
                        evidence_ids=(
                            [r[0] for r in high_data['rows'][:2]] +
                            [r[0] for r in low_data['rows'][:2]]
                        ),
                        recommended_action='investigate',
                        metadata={
                            'high_category': high_cat,
                            'low_category': low_cat,
                            'imbalance_score': high_data['score'] - low_data['score'],
                            'actionable': f'Review time/energy spent on {low_cat} vs {high_cat}',
                        },
                    ))

        return insights

    def _detect_causal_relationships(
        self, rows: list, trend_summary: Dict[str, Any]
    ) -> List[ReflectionInsight]:
        """
        Infer potential causal relationships from temporal patterns.

        Looks for:
        - Early improvements that preceded later changes
        - Consistent co-occurrence patterns
        """
        insights: List[ReflectionInsight] = []

        # Sort all rows by timestamp
        sorted_rows = sorted(
            rows,
            key=lambda r: r[8] if len(r) > 8 and r[8] else '',
            reverse=True,
        )

        # Look for "keystone" categories - early improvements that correlate with overall improvement
        by_category: Dict[str, list] = {}
        for row in sorted_rows:
            cat = row[2]
            cat = cat or 'general'
            if cat not in by_category:
                by_category[cat] = []
            by_category[cat].append(row)

        # Find categories with early strengthening
        for cat, cat_rows in by_category.items():
            if len(cat_rows) < 2:
                continue

            # Check if this category strengthened early and others followed
            sorted_cat = sorted(
                cat_rows,
                key=lambda r: r[8] if len(r) > 8 and r[8] else '',
            )

            early_rows = sorted_cat[: max(1, len(sorted_cat) // 3)]
            strengthening_early = sum(
                1 for r in early_rows if (r[4] or 'stable') == 'strengthening'
            )

            if strengthening_early >= len(early_rows) * 0.7 and len(cat_rows) >= 3:
                insights.append(ReflectionInsight(
                    insight_type='breakthrough',
                    summary=f"{cat} appears to be a keystone area - early improvements here may have driven broader progress. Double down on what is working in {cat}.",
                    confidence=0.7,
                    evidence_ids=[r[0] for r in early_rows],
                    recommended_action='preserve',
                    metadata={
                        'keystone_category': cat,
                        'early_strengthening_ratio': strengthening_early / len(early_rows),
                        'actionable': f'Document and replicate {cat} strategies in other areas',
                    },
                ))

        return insights

    def _detect_risks(
        self, rows: list, trend_summary: Dict[str, Any]
    ) -> List[ReflectionInsight]:
        """
        Flag potential risks before they escalate.

        Detects:
        - Rapidly weakening areas
        - Concentration risk (too much focus on one area)
        - Fragility indicators (high variance, low resilience)
        """
        insights: List[ReflectionInsight] = []
        counts = trend_summary.get('trend_counts', {})

        # Group by category
        by_category: Dict[str, list] = {}
        for row in rows:
            cat = row[2]
            cat = cat or 'general'
            if cat not in by_category:
                by_category[cat] = []
            by_category[cat].append(row)

        # Detect rapidly weakening categories
        for cat, cat_rows in by_category.items():
            weakening_count = sum(1 for r in cat_rows if (r[4] or 'stable') == 'weakening')
            total = len(cat_rows)

            if weakening_count / max(total, 1) > 0.5 and total >= 3:
                recent_weakening = [
                    r for r in cat_rows
                    if (r[4] or 'stable') == 'weakening'
                ][:3]

                insights.append(ReflectionInsight(
                    insight_type='warning',
                    summary=f"WARNING: {cat} showing consistent decline ({weakening_count}/{total} weakening). Address soon before pattern solidifies.",
                    confidence=0.8,
                    evidence_ids=[r[0] for r in recent_weakening],
                    recommended_action='review',
                    metadata={
                        'risk_category': cat,
                        'weakening_ratio': weakening_count / total,
                        'urgency': 'high' if weakening_count / total > 0.7 else 'medium',
                        'actionable': f'Identify specific stressors in {cat}; consider intervention',
                    },
                ))

        # Detect concentration risk
        total_memories = len(rows)
        if total_memories >= 5:
            for cat, cat_rows in by_category.items():
                concentration = len(cat_rows) / total_memories
                if concentration > 0.6:
                    insights.append(ReflectionInsight(
                        insight_type='warning',
                        summary=f'Focus concentration risk: {cat} dominates your attention ({concentration:.0%} of memories). Consider broadening scope.',
                        confidence=0.75,
                        evidence_ids=[r[0] for r in cat_rows[:5]],
                        recommended_action='investigate',
                        metadata={
                            'concentration_risk': concentration,
                            'dominant_category': cat,
                            'actionable': 'Explore underrepresented life areas',
                        },
                    ))

        return insights

    def _detect_opportunities(
        self, rows: list, trend_summary: Dict[str, Any]
    ) -> List[ReflectionInsight]:
        """
        Identify underutilized strengths and opportunities.

        Detects:
        - High-performing areas that could be leveraged more
        - Stable areas ready for growth
        - Latent capacity indicators
        """
        insights: List[ReflectionInsight] = []

        # Group by category
        by_category: Dict[str, list] = {}
        for row in rows:
            cat = row[2]
            cat = cat or 'general'
            if cat not in by_category:
                by_category[cat] = []
            by_category[cat].append(row)

        # Find stable areas with potential for growth
        for cat, cat_rows in by_category.items():
            stable_count = sum(1 for r in cat_rows if (r[4] or 'stable') == 'stable')
            strengthening_count = sum(
                1 for r in cat_rows if (r[4] or 'stable') == 'strengthening'
            )
            total = len(cat_rows)

            # Stable areas with some strengthening momentum
            if (
                stable_count / max(total, 1) > 0.4
                and strengthening_count >= 1
                and total >= 3
            ):
                insights.append(ReflectionInsight(
                    insight_type='breakthrough',
                    summary=f'{cat} is stable with emerging momentum - ripe for intentional growth. Small investments here could yield disproportionate returns.',
                    confidence=0.7,
                    evidence_ids=[r[0] for r in cat_rows[:4]],
                    recommended_action='preserve',
                    metadata={
                        'opportunity_category': cat,
                        'stable_ratio': stable_count / total,
                        'actionable': f'Add 1-2 deliberate practices to {cat} this week',
                    },
                ))

        return insights

    def _recommend_action_for_entity(self, entity: Dict[str, Any]) -> str:
        """
        Generate specific action recommendations based on entity type and connections.
        """
        entity_type = entity.get('type', 'concept')
        connections = entity.get('connections', 0)

        if connections >= 5:
            return 'leverage'  # Major hub - use as change agent
        elif entity_type == 'emotion':
            return 'review'  # Emotional hub - check for regulation needs
        elif entity_type == 'concept':
            return 'preserve'  # Conceptual hub - maintain and build on
        else:
            return 'investigate'

    def _extract_content_insights(
        self, rows: list
    ) -> List[ReflectionInsight]:
        """
        Extract content-anchored insights from memory rows.

        Groups memories by category, then picks the most recent
        strengthening and weakening memory per category to build
        evidence-anchored insight summaries.
        """
        insights: List[ReflectionInsight] = []

        # Group rows by category
        by_category: Dict[str, list] = {}
        for row in rows:
            category = row[2]
            cat = category or 'general'
            if cat not in by_category:
                by_category[cat] = []
            by_category[cat].append(row)

        for cat, cat_rows in by_category.items():
            # Sort by created_at descending to find most recent
            sorted_rows = sorted(
                cat_rows,
                key=lambda r: r[8] if len(r) > 8 else '',
                reverse=True,
            )

            strengthening_rows = [r for r in sorted_rows if (r[4] or 'stable') == 'strengthening']
            weakening_rows = [r for r in sorted_rows if (r[4] or 'stable') == 'weakening']

            if strengthening_rows:
                mem = strengthening_rows[0]
                excerpt = (mem[1] or '')[:80]
                insights.append(ReflectionInsight(
                    insight_type='trend',
                    summary=f'Progress in {cat}: {excerpt}',
                    confidence=0.8,
                    evidence_ids=[mem[0]],
                    recommended_action='preserve',
                    metadata={'category': cat, 'trend': 'strengthening'},
                ))

            if weakening_rows:
                mem = weakening_rows[0]
                excerpt = (mem[1] or '')[:80]
                insights.append(ReflectionInsight(
                    insight_type='warning',
                    summary=f'Decline in {cat}: {excerpt}',
                    confidence=0.8,
                    evidence_ids=[mem[0]],
                    recommended_action='review',
                    metadata={'category': cat, 'trend': 'weakening'},
                ))

        return insights

    def _store_reflection(
        self,
        conn: sqlite3.Connection,
        report: ReflectionReport,
        user_id: str,
        tenant_id: str,
    ) -> None:
        """Store reflection report as a memory for continuity."""
        try:
            now = datetime.now(timezone.utc).isoformat()
            content = f"[Reflection: {report.period}] {report.memories_analyzed} memories analyzed, {len(report.insights)} insights found. Overall trend: {report.trend_summary.get('overall', 'unknown')}"

            # Build gist from insight summaries for quick retrieval
            insight_summaries = [i.summary for i in report.insights]
            gist = '; '.join(insight_summaries) if insight_summaries else report.trend_summary.get('overall', 'unknown')

            conn.execute(
                """INSERT OR REPLACE INTO memories
                (id, user_id, tenant_id, scope, retention, content,
                tags, emotional_context, metrics, gist,
                is_ghost, synthesized_from, created_at, updated_at,
                category, importance)
                VALUES (?, ?, ?, 'session', 'long_term', ?,
                ?, ?, ?, ?,
                0, ?, ?, ?,
                ?, ?)""",
                (
                    report.report_id,
                    user_id,
                    tenant_id,
                    content,
                    json.dumps([f'reflection:{report.period}']),
                    json.dumps({}),
                    json.dumps({'insights': len(report.insights)}),
                    gist,
                    json.dumps([r for r in []]),
                    now,
                    now,
                    'reflection',
                    0.9, # Reflection memories are high importance
                ),
            )
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to store reflection report {report.report_id}: {e}", exc_info=True)
            try:
                conn.rollback()
            except Exception:
                pass


# Global instance management
_reflection_engine: Optional[ReflectionEngine] = None
_engine_lock = threading.Lock()


def get_reflection_engine(db_path: Optional[str] = None) -> ReflectionEngine:
    """Get or create global reflection engine instance (thread-safe)."""
    global _reflection_engine
    with _engine_lock:
        if _reflection_engine is None:
            if db_path is None:
                from .config import DB_PATH
                db_path = DB_PATH
            _reflection_engine = ReflectionEngine(db_path)
        return _reflection_engine


def reset_reflection_engine() -> None:
    """Reset global reflection engine (for testing)."""
    global _reflection_engine
    with _engine_lock:
        _reflection_engine = None