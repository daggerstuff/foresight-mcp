"""
Crisis Detection Service
Detects psychological crisis signals in user content for safety intervention.
Restored from src/lib/ai/services/crisis-detection.ts
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional, Literal
import re


RiskLevel = Literal['low', 'moderate', 'high', 'critical']
CrisisCategory = Literal['self_harm', 'depression', 'anxiety', 'trauma',
                         'substance_abuse', 'eating_disorder', 'crisis_event']


@dataclass
class CrisisDetectionResult:
    """Result of crisis detection analysis."""
    is_crisis: bool
    category: Optional[CrisisCategory]
    risk_level: RiskLevel
    confidence: float
    urgency: Literal['routine', 'elevated', 'high', 'immediate']
    detected_terms: List[str]
    recommended_action: str

    def to_dict(self) -> dict:
        return {
            "is_crisis": self.is_crisis,
            "category": self.category,
            "risk_level": self.risk_level,
            "confidence": self.confidence,
            "urgency": self.urgency,
            "detected_terms": self.detected_terms,
            "recommended_action": self.recommended_action,
        }


# Crisis keyword patterns by category
CRISIS_PATTERNS = {
    'self_harm': {
        'keywords': [
            r'\bkill myself\b', r'\bend my life\b', r'\bsuicide\b',
            r'\bself harm\b', r'\bhurt myself\b', r'\bcut myself\b',
            r'\bdont want to live\b', r'\blife is not worth living\b',
            r'\bwish i was dead\b', r'\bno reason to live\b',
        ],
        'urgency': 'immediate',
        'risk_level': 'critical',
    },
    'depression': {
        'keywords': [
            r'\bso hopeless\b', r'\bnothing matters\b', r'\bempty inside\b',
            r'\bcant feel anything\b', r'\bworthless\b', r'\bburden to everyone\b',
            r'\bno point\b', r'\bdeeply depressed\b', r'\bsevere depression\b',
        ],
        'urgency': 'high',
        'risk_level': 'high',
    },
    'anxiety': {
        'keywords': [
            r'\bpanic attack\b', r'\bcant breathe\b', r'\bheart racing\b',
            r'\boverwhelming anxiety\b', r'\bsevere anxiety\b', r'\bcan t function\b',
        ],
        'urgency': 'elevated',
        'risk_level': 'moderate',
    },
    'trauma': {
        'keywords': [
            r'\bflashback\b', r'\bptsd episode\b', r'\btrauma response\b',
            r'\bdissociating\b', r'\btriggered by\b',
        ],
        'urgency': 'elevated',
        'risk_level': 'moderate',
    },
    'substance_abuse': {
        'keywords': [
            r'\brelapsed\b', r'\boverdosed\b', r'\bcan t stop using\b',
            r'\baddicted to\b', r'\bwithdrawal symptoms\b',
        ],
        'urgency': 'high',
        'risk_level': 'high',
    },
    'eating_disorder': {
        'keywords': [
            r'\bstarving myself\b', r'\bpurge\b', r'\bbinge eating\b',
            r'\bpro ana\b', r'\bmia\b', r'\bed recovery\b',
        ],
        'urgency': 'elevated',
        'risk_level': 'moderate',
    },
    'crisis_event': {
        'keywords': [
            r'\bin crisis\b', r'\bmental health crisis\b', r'\bbreaking point\b',
            r'\bcant go on\b', r'\blosing my mind\b',
        ],
        'urgency': 'immediate',
        'risk_level': 'critical',
    },
}


class CrisisDetectionService:
    """
    Hybrid Crisis Detection Service
    Combines keyword pattern matching with configurable sensitivity.
    """

    def __init__(self, sensitivity_level: Literal['low', 'medium', 'high'] = 'medium'):
        self.sensitivity_level = sensitivity_level
        self._compile_patterns()

    def _compile_patterns(self):
        """Pre-compile regex patterns for performance."""
        self.compiled_patterns = {}
        for category, config in CRISIS_PATTERNS.items():
            self.compiled_patterns[category] = [
                re.compile(pattern, re.IGNORECASE)
                for pattern in config['keywords']
            ]

    def detect_crisis(self, content: str,
                      sensitivity_level: Optional[Literal['low', 'medium', 'high']] = None,
                      user_id: str = "default",
                      source: str = "memory_tagger") -> CrisisDetectionResult:
        """
        Detect crisis signals in content.

        Args:
            content: The text content to analyze
            sensitivity_level: Override default sensitivity
            user_id: User ID for tracking
            source: Source of the content

        Returns:
            CrisisDetectionResult with findings
        """
        sensitivity = sensitivity_level or self.sensitivity_level
        detected_terms = []
        category_scores = {}

        # Scan for crisis patterns
        for category, patterns in self.compiled_patterns.items():
            matches = []
            for pattern in patterns:
                matches.extend(pattern.findall(content))

            if matches:
                detected_terms.extend([m.strip() for m in matches])
                config = CRISIS_PATTERNS[category]
                category_scores[category] = {
                    'count': len(matches),
                    'urgency': config['urgency'],
                    'risk_level': config['risk_level'],
                }

        # Determine if crisis detected
        is_crisis = len(detected_terms) > 0
        primary_category = None
        risk_level: RiskLevel = 'low'
        urgency: Literal['routine', 'elevated', 'high', 'immediate'] = 'routine'
        confidence = 0.0
        recommended_action = "No intervention required."

        if is_crisis:
            # Find primary category (highest severity)
            severity_order = {'critical': 4, 'high': 3, 'moderate': 2, 'low': 1}
            primary_category = max(
                category_scores.keys(),
                key=lambda c: severity_order.get(category_scores[c]['risk_level'], 0)
            )

            primary_config = category_scores[primary_category]
            risk_level = primary_config['risk_level']
            urgency = primary_config['urgency']

            # Calculate confidence based on match count and sensitivity
            base_confidence = min(len(detected_terms) * 0.3, 0.9)
            sensitivity_boost = {'high': 0.1, 'medium': 0.05, 'low': 0.0}
            confidence = min(base_confidence + sensitivity_boost.get(sensitivity, 0), 1.0)

            # Set recommended action
            if urgency == 'immediate':
                recommended_action = (
                    "IMMEDIATE INTERVENTION REQUIRED: "
                    "Escalate to human supervisor immediately. "
                    "Do not continue session without professional review."
                )
            elif urgency == 'high':
                recommended_action = (
                    "HIGH PRIORITY: Flag for urgent supervisor review. "
                    "Prepare crisis resources for user."
                )
            elif urgency == 'elevated':
                recommended_action = (
                    "ELEVATED CONCERN: Include in post-session summary. "
                    "Monitor closely for escalation."
                )
        else:
            confidence = 0.0

        return CrisisDetectionResult(
            is_crisis=is_crisis,
            category=primary_category,
            risk_level=risk_level,
            confidence=confidence,
            urgency=urgency,
            detected_terms=list(set(detected_terms)),
            recommended_action=recommended_action,
        )


# Global instance
_crisis_service: Optional[CrisisDetectionService] = None


def get_crisis_service(sensitivity: Literal['low', 'medium', 'high'] = 'medium') -> CrisisDetectionService:
    """Get or create the global crisis detection service instance."""
    global _crisis_service
    if _crisis_service is None:
        _crisis_service = CrisisDetectionService(sensitivity_level=sensitivity)
    return _crisis_service
