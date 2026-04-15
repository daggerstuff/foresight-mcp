"""
Anomaly Detection System
Domain-agnostic anomaly detection with pluggable strategies.
Extensible to any domain (mental health, security, finance, etc.)
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional, Literal, Dict, Any
import re


# =============================================================================
# Core Types (Domain-Agnostic)
# =============================================================================

RiskLevel = Literal['none', 'low', 'moderate', 'high', 'critical']
Urgency = Literal['routine', 'elevated', 'high', 'immediate']


@dataclass
class AnomalyResult:
    """
    Result of anomaly detection analysis.

    Generic enough to handle any domain:
    - Mental health: crisis detection
    - Security: intrusion detection
    - Finance: fraud detection
    - DevOps: anomaly detection
    """
    is_anomaly: bool
    category: Optional[str]
    risk_level: RiskLevel
    confidence: float
    urgency: Urgency
    detected_terms: List[str]
    recommended_action: str
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}

    def to_dict(self) -> dict:
        return {
            "is_anomaly": self.is_anomaly,
            "category": self.category,
            "risk_level": self.risk_level,
            "confidence": self.confidence,
            "urgency": self.urgency,
            "detected_terms": self.detected_terms,
            "recommended_action": self.recommended_action,
            "metadata": self.metadata,
        }


class AnomalyDetector(ABC):
    """
    Abstract base class for anomaly detectors.

    Implement this ABC to create domain-specific detectors:
    - MentalHealthAnomalyDetector
    - SecurityAnomalyDetector
    - FinanceAnomalyDetector
    - DevOpsAnomalyDetector
    """

    @abstractmethod
    def detect(self, content: str, **kwargs) -> AnomalyResult:
        """
        Detect anomalies in content.

        Args:
            content: The text content to analyze
            **kwargs: Domain-specific parameters

        Returns:
            AnomalyResult with findings
        """
        pass

    @abstractmethod
    def get_categories(self) -> List[str]:
        """Return list of anomaly categories this detector supports."""
        pass


# =============================================================================
# Mental Health Implementation (Legacy Crisis Detection)
# =============================================================================

MentalHealthCategory = Literal[
    'self_harm', 'depression', 'anxiety', 'trauma',
    'substance_abuse', 'eating_disorder', 'crisis_event'
]

# Crisis keyword patterns by category - mental health specific
MENTAL_HEALTH_PATTERNS = {
    'self_harm': {
        'keywords': [
            r'\bkill myself\b', r'\bend my life\b', r'\bsuicide\b',
            r'\bsuicidal\b', r'\bself harm\b', r'\bhurt myself\b',
            r'\bcut myself\b', r'\bdont want to live\b',
            r'\blife is not worth living\b', r'\bwish i was dead\b',
            r'\bno reason to live\b', r'\bwant to die\b',
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


class MentalHealthAnomalyDetector(AnomalyDetector):
    """
    Mental health anomaly detector.

    Detects crisis signals in user content for safety intervention.
    This is the legacy CrisisDetectionService refactored to use AnomalyDetector ABC.
    """

    def __init__(self, sensitivity_level: Literal['low', 'medium', 'high'] = 'medium'):
        self.sensitivity_level = sensitivity_level
        self._compile_patterns()

    def _compile_patterns(self):
        """Pre-compile regex patterns for performance."""
        self.compiled_patterns = {}
        for category, config in MENTAL_HEALTH_PATTERNS.items():
            self.compiled_patterns[category] = [
                re.compile(pattern, re.IGNORECASE)
                for pattern in config['keywords']
            ]

    def detect(self, content: str, **kwargs) -> AnomalyResult:
        """
        Detect mental health crisis signals in content.

        Args:
            content: The text content to analyze
            sensitivity_level: Override default sensitivity (optional)
            user_id: User ID for tracking (optional)
            source: Source of the content (optional)

        Returns:
            AnomalyResult with findings
        """
        sensitivity = kwargs.get('sensitivity_level', self.sensitivity_level)
        detected_terms = []
        category_scores = {}

        # Scan for crisis patterns
        for category, patterns in self.compiled_patterns.items():
            matches = []
            for pattern in patterns:
                matches.extend(pattern.findall(content))

            if matches:
                detected_terms.extend([m.strip() for m in matches])
                config = MENTAL_HEALTH_PATTERNS[category]
                category_scores[category] = {
                    'count': len(matches),
                    'urgency': config['urgency'],
                    'risk_level': config['risk_level'],
                }

        # Determine if anomaly detected
        is_anomaly = len(detected_terms) > 0
        primary_category = None
        risk_level: RiskLevel = 'none'
        urgency: Urgency = 'routine'
        confidence = 0.0
        recommended_action = "No intervention required."

        if is_anomaly:
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

        return AnomalyResult(
            is_anomaly=is_anomaly,
            category=primary_category,
            risk_level=risk_level,
            confidence=confidence,
            urgency=urgency,
            detected_terms=list(set(detected_terms)),
            recommended_action=recommended_action,
            metadata={'sensitivity': sensitivity, 'source': kwargs.get('source')}
        )

    def get_categories(self) -> List[str]:
        """Return list of mental health categories this detector supports."""
        return list(MENTAL_HEALTH_PATTERNS.keys())


# =============================================================================
# Backward Compatibility Layer
# =============================================================================

class CrisisDetectionService(MentalHealthAnomalyDetector):
    """
    Backward compatibility alias.

    DEPRECATED: Use MentalHealthAnomalyDetector instead.
    This alias exists for backward compatibility with existing code.
    """
    pass


# =============================================================================
# Global Instance Management
# =============================================================================

_anomaly_detector: Optional[AnomalyDetector] = None
_detector_type: str = 'mental_health'


def get_anomaly_detector(
    detector_type: str = 'mental_health',
    **kwargs
) -> AnomalyDetector:
    """
    Get or create an anomaly detector instance.

    Args:
        detector_type: Type of detector ('mental_health', 'security', 'finance', etc.)
        **kwargs: Detector-specific configuration

    Returns:
        Configured AnomalyDetector instance
    """
    global _anomaly_detector, _detector_type

    # Return existing if same type
    if _anomaly_detector is not None and _detector_type == detector_type:
        return _anomaly_detector

    # Create new detector
    if detector_type == 'mental_health':
        sensitivity = kwargs.get('sensitivity', 'medium')
        _anomaly_detector = MentalHealthAnomalyDetector(sensitivity_level=sensitivity)
    else:
        raise ValueError(f"Unknown detector type: {detector_type}")

    _detector_type = detector_type
    return _anomaly_detector


# Legacy function for backward compatibility
def get_crisis_service(sensitivity: Literal['low', 'medium', 'high'] = 'medium') -> MentalHealthAnomalyDetector:
    """
    Get crisis detection service (legacy name).

    DEPRECATED: Use get_anomaly_detector() instead.
    This function exists for backward compatibility.
    """
    return get_anomaly_detector(detector_type='mental_health', sensitivity=sensitivity)
