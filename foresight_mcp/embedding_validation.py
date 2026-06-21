"""Embedding Dimension Validation.

Provides validation for embedding vectors to prevent silent data corruption
from mismatched embedding dimensions.

Supported embedding models and their dimensions:
- text-embedding-ada-002: 1536
- text-embedding-3-small: 1536
- text-embedding-3-large: 3072
- bge-large-en-v1.5: 1024
- all-MiniLM-L6-v2: 384
"""

import math
from dataclasses import dataclass
from typing import Literal

# Supported embedding models and their dimensions
EMBEDDING_DIMENSIONS = {
    "text-embedding-ada-002": 1536,
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "bge-large-en-v1.5": 1024,
    "bge-small-en-v1.5": 384,
    "all-MiniLM-L6-v2": 384,
    "all-mpnet-base-v2": 768,
}

EmbeddingModel = Literal[
    "text-embedding-ada-002",
    "text-embedding-3-small",
    "text-embedding-3-large",
    "bge-large-en-v1.5",
    "bge-small-en-v1.5",
    "all-MiniLM-L6-v2",
    "all-mpnet-base-v2",
]


class EmbeddingDimensionError(ValueError):
    """Raised when embedding dimension validation fails."""


@dataclass
class EmbeddingConfig:
    """Configuration for embedding validation."""

    model: EmbeddingModel
    expected_dimension: int

    def __post_init__(self):
        """Validate model and set expected dimension."""
        if self.model not in EMBEDDING_DIMENSIONS:
            valid_models = ", ".join(EMBEDDING_DIMENSIONS.keys())
            raise ValueError(f"Unknown embedding model: {self.model}. Valid models: {valid_models}")
        # Override with known dimension
        self.expected_dimension = EMBEDDING_DIMENSIONS[self.model]


def validate_embedding_dimension(
    vector: list[float],
    expected_dimension: int | None = None,
    model: EmbeddingModel | None = None,
) -> tuple[bool, str]:
    """
    Validate that an embedding vector has the correct dimension.

    Args:
        vector: The embedding vector to validate
        expected_dimension: Expected dimension (required if model not provided)
        model: Embedding model name (dimension will be looked up)

    Returns:
        Tuple of (is_valid, error_message)

    Raises:
        EmbeddingDimensionError: If validation fails
    """
    actual_dimension = len(vector)

    # Check for NaN or infinity values
    if any(math.isnan(v) or math.isinf(v) for v in vector):
        raise EmbeddingDimensionError(
            f"Vector contains NaN or Infinity values at position "
            f"{next(i for i, v in enumerate(vector) if math.isnan(v) or math.isinf(v))}"
        )

    # Determine expected dimension
    if model is not None:
        expected_dimension = EMBEDDING_DIMENSIONS.get(model)
        if expected_dimension is None:
            valid_models = ", ".join(EMBEDDING_DIMENSIONS.keys())
            raise EmbeddingDimensionError(f"Unknown embedding model: {model}. Valid models: {valid_models}")
    elif expected_dimension is None:
        raise EmbeddingDimensionError("Either model or expected_dimension must be provided")

    # Validate dimension
    if actual_dimension != expected_dimension:
        raise EmbeddingDimensionError(
            f"Embedding dimension mismatch: expected {expected_dimension}, "
            f"got {actual_dimension}. "
            f"Vector length: {len(vector)}"
        )

    return True, ""


def validate_embedding_vectors(
    vectors: list[list[float]],
    expected_dimension: int | None = None,
    model: EmbeddingModel | None = None,
) -> list[str]:
    """
    Validate a batch of embedding vectors.

    Args:
        vectors: List of embedding vectors to validate
        expected_dimension: Expected dimension (required if model not provided)
        model: Embedding model name (dimension will be looked up)

    Returns:
        List of error messages (empty if all valid)
    """
    errors = []

    for i, vector in enumerate(vectors):
        try:
            validate_embedding_dimension(vector, expected_dimension, model)
        except EmbeddingDimensionError as e:
            errors.append(f"Vector {i}: {e!s}")

    return errors


def get_embedding_dimension(model: EmbeddingModel) -> int:
    """
    Get the expected dimension for an embedding model.

    Args:
        model: Embedding model name

    Returns:
        Expected dimension for the model
    """
    return EMBEDDING_DIMENSIONS.get(model, 0)


# Default configuration for the project
DEFAULT_EMBEDDING_MODEL: EmbeddingModel = "text-embedding-ada-002"
DEFAULT_EMBEDDING_DIMENSION = EMBEDDING_DIMENSIONS[DEFAULT_EMBEDDING_MODEL]
