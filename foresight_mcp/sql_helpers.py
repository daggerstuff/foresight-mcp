"""
SQL Helpers - Safe SQL generation utilities.

These helpers validate identifiers and generate safe SQL with parameterized queries.
"""

# Whitelist of valid relationship types - from graph_store.py schema
VALID_RELATIONSHIP_TYPES = frozenset(
    {"mentions", "located_at", "experienced", "caused", "relates_to", "contradicts", "supports", "part_of", "created"}
)

# Whitelist of valid entity types - from graph_store.py schema
VALID_ENTITY_TYPES = frozenset({"person", "place", "concept", "event", "emotion", "object"})

# Whitelist of valid table names for internal use
VALID_TABLE_NAMES = frozenset(
    {
        "memory_entities",
        "entity_relationships",
        "memory_entity_links",
        "memories",
        "tenants",
        "decay_config",
        "memory_versions",
        "schema_migrations",
    }
)


def build_type_filter(relationship_types: list[str]) -> tuple[str, list[str]]:
    """Build a safe type filter clause with validated relationship types.

    Args:
        relationship_types: List of relationship type names to validate and use

    Returns:
        Tuple of (sql_fragment, params) where sql_fragment is like
        "AND r.relationship_type IN (?,?,?)" and params is the validated list

    Raises:
        ValueError: If any relationship type is invalid
    """
    if not relationship_types:
        return ("", [])

    # Validate each type
    validated_types = []
    for rt in relationship_types:
        validate_identifier(rt, VALID_RELATIONSHIP_TYPES, "relationship_type")
        validated_types.append(rt)

    placeholders = ",".join("?" * len(validated_types))
    sql = f"AND r.relationship_type IN ({placeholders})"
    return (sql, validated_types)


def validate_identifier(name: str, valid_set: frozenset, context: str = "value") -> str:
    """Validate that an identifier is in the allowed set.

    Args:
        name: The identifier to validate
        valid_set: Set of allowed values
        context: Description for error message (e.g., "table", "relationship type")

    Returns:
        The validated name

    Raises:
        ValueError: If name is not in valid_set
    """
    if not isinstance(name, str):
        raise ValueError(f"{context} must be a string, got {type(name).__name__}")
    if name not in valid_set:
        raise ValueError(f"Invalid {context}: {name!r}. Must be one of: {sorted(valid_set)}")
    return name


def build_in_clause(values: list[str], valid_set: frozenset | None = None) -> tuple[str, list[str]]:
    """Build a safe IN clause with placeholders.

    Args:
        values: List of string values to include
        valid_set: Optional validation set to check against

    Returns:
        Tuple of (placeholder_string, validated_values)
        Example: ("?,?,?", ["val1", "val2", "val3"])

    Raises:
        ValueError: If any value fails validation
    """
    if not values:
        return ("", [])

    # Validate each value if valid_set is provided
    validated_values = []
    for value in values:
        if valid_set is not None:
            validate_identifier(value, valid_set, "value")
        validated_values.append(value)

    placeholders = ",".join("?" * len(validated_values))
    return (placeholders, validated_values)


def is_valid_entity_type(entity_type: str) -> bool:
    """Check if entity type is valid."""
    return entity_type in VALID_ENTITY_TYPES


def is_valid_relationship_type(rel_type: str) -> bool:
    """Check if relationship type is valid."""
    return rel_type in VALID_RELATIONSHIP_TYPES


def is_valid_table_name(table_name: str) -> bool:
    """Check if table name is valid (for internal schema operations only)."""
    return table_name in VALID_TABLE_NAMES
