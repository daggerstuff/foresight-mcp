"""RRF Weight Tuning and Configuration.

Provides configurable RRF (Reciprocal Rank Fusion) weights and
grid search utilities for optimizing retrieval performance.

Current default weights (subject to tuning):
- keyword: 1.0 (primary relevance signal)
- tfidf_cosine: 0.7 (topical similarity via TF-IDF)
- graph: 0.8 (entity expansion value)
- temporal: 0.6 (recency context)
- RRF k: 60 (standard smoothing constant)
"""
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("foresight_rrf_tuning")


@dataclass
class RRFConfig:
    """Configuration for RRF fusion weights."""
    rrf_k: float = 60.0  # Smoothing constant
    keyword_weight: float = 1.0
    tfidf_cosine_weight: float = 0.7
    graph_weight: float = 0.8
    temporal_weight: float = 0.6

    def to_dict(self) -> dict[str, float]:
        """Convert to dictionary for storage."""
        return {
            "rrf_k": self.rrf_k,
            "keyword": self.keyword_weight,
            "tfidf_cosine": self.tfidf_cosine_weight,
            "graph": self.graph_weight,
            "temporal": self.temporal_weight,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RRFConfig":
        """Create from dictionary."""
        return cls(
            rrf_k=data.get("rrf_k", 60.0),
            keyword_weight=data.get("keyword", 1.0),
            tfidf_cosine_weight=data.get("tfidf_cosine", 0.7),
            graph_weight=data.get("graph", 0.8),
            temporal_weight=data.get("temporal", 0.6),
        )

    def to_json_file(self, path: str) -> None:
        """Save configuration to JSON file."""
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def from_json_file(cls, path: str) -> "RRFConfig":
        """Load configuration from JSON file."""
        with open(path, "r") as f:
            data = json.load(f)
        return cls.from_dict(data)


# Default configuration file location
DEFAULT_CONFIG_PATH = Path.home() / ".foresight" / "rrf_config.json"


def get_rrf_config(config_path: str | None = None) -> RRFConfig:
    """
    Get RRF configuration from file or return defaults.

    Args:
        config_path: Path to config file (default: ~/.foresight/rrf_config.json)

    Returns:
        RRFConfig with loaded or default values
    """
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH

    if path.exists():
        try:
            config = RRFConfig.from_json_file(str(path))
            logger.info(f"Loaded RRF config from {path}")
            return config
        except Exception as e:
            logger.warning(f"Failed to load RRF config: {e}. Using defaults.")

    return RRFConfig()


def save_rrf_config(config: RRFConfig, config_path: str | None = None) -> None:
    """
    Save RRF configuration to file.

    Args:
        config: Configuration to save
        config_path: Path to config file (default: ~/.foresight/rrf_config.json)
    """
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    config.to_json_file(str(path))
    logger.info(f"Saved RRF config to {path}")


@dataclass
class GridSearchResult:
    """Result from grid search optimization."""
    best_weights: dict[str, float]
    best_score: float
    all_results: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "best_weights": self.best_weights,
            "best_score": self.best_score,
            "num_evaluated": len(self.all_results),
        }


def grid_search_weights(
    keyword_range: tuple[float, float, float] = (0.5, 2.0, 0.5),
    tfidf_range: tuple[float, float, float] = (0.3, 1.0, 0.2),
    graph_range: tuple[float, float, float] = (0.4, 1.2, 0.2),
    temporal_range: tuple[float, float, float] = (0.2, 1.0, 0.2),
    evaluate_fn: Optional[Callable[[dict[str, float]], float]] = None,
) -> GridSearchResult:
    """
    Grid search for optimal RRF weights.

    Args:
        keyword_range: (min, max, step) for keyword weight
        tfidf_range: (min, max, step) for tfidf_cosine weight
        graph_range: (min, max, step) for graph weight
        temporal_range: (min, max, step) for temporal weight
        evaluate_fn: Function that takes weights dict and returns score

    Returns:
        GridSearchResult with best weights and all results

    Note:
        evaluate_fn should be provided for actual tuning.
        Without it, returns placeholder results.
    """
    import itertools

    keyword_values = list(
        frange(keyword_range[0], keyword_range[1] + 0.001, keyword_range[2])
    )
    tfidf_values = list(
        frange(tfidf_range[0], tfidf_range[1] + 0.001, tfidf_range[2])
    )
    graph_values = list(
        frange(graph_range[0], graph_range[1] + 0.001, graph_range[2])
    )
    temporal_values = list(
        frange(temporal_range[0], temporal_range[1] + 0.001, temporal_range[2])
    )

    all_combinations = list(
        itertools.product(keyword_values, tfidf_values, graph_values, temporal_values)
    )

    best_score = float("-inf")
    best_weights: dict[str, float] = {}
    all_results: list[dict[str, Any]] = []

    for kw, tfidf, graph, temporal in all_combinations:
        weights = {
            "keyword": kw,
            "tfidf_cosine": tfidf,
            "graph": graph,
            "temporal": temporal,
        }

        if evaluate_fn:
            score = evaluate_fn(weights)
        else:
            # Placeholder - would need actual evaluation data
            score = 0.0

        all_results.append({"weights": weights, "score": score})

        if score > best_score:
            best_score = score
            best_weights = weights

    return GridSearchResult(
        best_weights=best_weights,
        best_score=best_score,
        all_results=all_results,
    )


def frange(start: float, stop: float, step: float) -> list[float]:
    """Float range generator."""
    result = []
    value = start
    while value <= stop:
        result.append(round(value, 4))
        value += step
    return result


# A/B Testing support
@dataclass
class ABTestConfig:
    """Configuration for A/B testing different weight sets."""
    test_name: str
    control_weights: RRFConfig
    variant_weights: dict[str, list[RRFConfig]]  # variant_name -> [configs]
    traffic_split: dict[str, float]  # variant_name -> percentage

    def validate(self) -> bool:
        """Validate traffic split sums to 100."""
        total = sum(self.traffic_split.values())
        return abs(total - 100.0) < 0.01


def select_variant(
    test_config: ABTestConfig,
    user_id: str,
) -> tuple[str, RRFConfig]:
    """
    Select weight variant based on user ID for consistent A/B testing.

    Args:
        test_config: A/B test configuration
        user_id: User ID for consistent assignment

    Returns:
        Tuple of (variant_name, RRFConfig)
    """
    import hashlib

    # Hash user ID to get consistent assignment
    hash_value = int(hashlib.md5(user_id.encode()).hexdigest(), 16)
    roll = (hash_value % 100) + 1

    cumulative = 0.0
    for variant, percentage in test_config.traffic_split.items():
        cumulative += percentage
        if roll <= cumulative:
            # Return first variant config (simplified - would pick based on user segment)
            configs = test_config.variant_weights.get(variant, [test_config.control_weights])
            return (variant, configs[0])

    # Default to control
    return ("control", test_config.control_weights)
