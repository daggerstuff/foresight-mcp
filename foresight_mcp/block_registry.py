"""
Memory Block Registry and Schema System
Composable memory blocks with dynamic registration and validation.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Callable, Dict, List, Literal
from enum import Enum


# =============================================================================
# Enums
# =============================================================================

class RetentionPolicy(Enum):
    """Defines how long a memory block is retained."""
    EPHEMERAL = "ephemeral"      # Deleted after session
    SHORT_TERM = "short_term"    # Kept for duration of arc
    LONG_TERM = "long_term"      # Candidate for archival
    PERMANENT = "permanent"      # Never archived


class MergeStrategy(Enum):
    """Defines how new content is merged with existing content."""
    APPEND = "append"           # Append to existing content
    REPLACE = "replace"         # Replace entire content
    SYNTHESIZE = "synthesize"   # LLM-based synthesis


class InjectionPoint(Enum):
    """Defines where block content is injected in prompts."""
    PRE_PROMPT = "pre_prompt"       # Inject at start of prompt
    POST_PROMPT = "post_prompt"     # Inject at end of prompt
    WHISPER_ONLY = "whisper_only"   # Only in whisper injections


class BlockScope(Enum):
    """Defines the scope of a memory block."""
    GLOBAL = "global"         # Global across all projects
    PROJECT = "project"       # Specific to a project
    SESSION = "session"       # Specific to a session


# =============================================================================
# Schema Definition
# =============================================================================

@dataclass
class MemoryBlockSchema:
    """
    Schema for a memory block definition.

    Attributes:
        label: Unique identifier for the block
        description: Human-readable description
        retention_policy: How long the block is retained
        merge_strategy: How content is merged
        injection_point: Where content is injected
        scope: Scope of the block
        char_limit: Maximum character limit (0 = unlimited)
        validator: Optional validation function
        metadata: Additional metadata
    """
    label: str
    description: str = ""
    retention_policy: RetentionPolicy = RetentionPolicy.SHORT_TERM
    merge_strategy: MergeStrategy = MergeStrategy.APPEND
    injection_point: InjectionPoint = InjectionPoint.PRE_PROMPT
    scope: BlockScope = BlockScope.SESSION
    char_limit: int = 0
    validator: Optional[Callable[[str], bool]] = None
    metadata: Dict = field(default_factory=dict)

    def validate_content(self, content: str) -> tuple[bool, str]:
        """
        Validate content against this schema.

        Returns:
            Tuple of (is_valid, error_message)
        """
        # Check character limit
        if self.char_limit > 0 and len(content) > self.char_limit:
            return False, f"Content exceeds char limit ({len(content)} > {self.char_limit})"

        # Run custom validator
        if self.validator and not self.validator(content):
            return False, "Custom validation failed"

        return True, ""

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "label": self.label,
            "description": self.description,
            "retention_policy": self.retention_policy.value,
            "merge_strategy": self.merge_strategy.value,
            "injection_point": self.injection_point.value,
            "scope": self.scope.value,
            "char_limit": self.char_limit,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> MemoryBlockSchema:
        """Create from dictionary."""
        return cls(
            label=data["label"],
            description=data.get("description", ""),
            retention_policy=RetentionPolicy(data.get("retention_policy", "short_term")),
            merge_strategy=MergeStrategy(data.get("merge_strategy", "append")),
            injection_point=InjectionPoint(data.get("injection_point", "pre_prompt")),
            scope=BlockScope(data.get("scope", "session")),
            char_limit=data.get("char_limit", 0),
            validator=None,  # Validators can't be serialized
            metadata=data.get("metadata", {}),
        )


# =============================================================================
# Block Instance
# =============================================================================

@dataclass
class MemoryBlock:
    """
    An instance of a memory block with content.

    Attributes:
        schema: The block schema
        content: The block content
        created_at: Creation timestamp
        updated_at: Last update timestamp
        version: Version number for conflict detection
    """
    schema: MemoryBlockSchema
    content: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    version: int = 0

    def update_content(self, new_content: str) -> None:
        """Update content with merge strategy."""
        if self.schema.merge_strategy == MergeStrategy.REPLACE:
            self.content = new_content
        elif self.schema.merge_strategy == MergeStrategy.APPEND:
            if self.content:
                self.content = f"{self.content}\n{new_content}"
            else:
                self.content = new_content
        else:
            # Synthesize would require LLM - for now just replace
            self.content = new_content

        self.updated_at = datetime.now(timezone.utc)
        self.version += 1

    def is_empty(self) -> bool:
        """Check if block is empty or in default state."""
        return not self.content.strip() or self.content.startswith("(No")

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "schema": self.schema.to_dict(),
            "content": self.content,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "version": self.version,
        }


# =============================================================================
# Block Registry (Singleton)
# =============================================================================

class BlockRegistry:
    """
    Registry for memory block schemas.

    Singleton pattern - use get_registry() to get instance.
    """

    _instance: Optional["BlockRegistry"] = None

    def __new__(cls) -> "BlockRegistry":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._schemas: Dict[str, MemoryBlockSchema] = {}
        self._blocks: Dict[str, MemoryBlock] = {}
        self._initialized = True

    def register(self, schema: MemoryBlockSchema) -> None:
        """Register a block schema."""
        if schema.label in self._schemas:
            raise ValueError(f"Block schema '{schema.label}' already registered")
        self._schemas[schema.label] = schema

    def get_schema(self, label: str) -> Optional[MemoryBlockSchema]:
        """Get schema by label."""
        return self._schemas.get(label)

    def list_schemas(self) -> List[MemoryBlockSchema]:
        """List all registered schemas."""
        return list(self._schemas.values())

    def create_block(self, label: str, content: str = "") -> MemoryBlock:
        """Create a new block instance from a schema."""
        schema = self.get_schema(label)
        if not schema:
            raise ValueError(f"Block schema '{label}' not found")
        return MemoryBlock(schema=schema, content=content)

    def get_block(self, label: str) -> Optional[MemoryBlock]:
        """Get block instance by label."""
        return self._blocks.get(label)

    def set_block(self, label: str, block: MemoryBlock) -> None:
        """Set a block instance."""
        self._blocks[label] = block

    def list_blocks(self) -> List[MemoryBlock]:
        """List all block instances."""
        return list(self._blocks.values())

    def delete_block(self, label: str) -> bool:
        """Delete a block instance."""
        if label in self._blocks:
            del self._blocks[label]
            return True
        return False

    def clear(self) -> None:
        """Clear all blocks (not schemas)."""
        self._blocks.clear()


# =============================================================================
# Default Block Schemas
# =============================================================================

DEFAULT_BLOCK_SCHEMAS = [
    MemoryBlockSchema(
        label="core_directives",
        description="Role definition and operating principles",
        retention_policy=RetentionPolicy.PERMANENT,
        merge_strategy=MergeStrategy.REPLACE,
        injection_point=InjectionPoint.PRE_PROMPT,
        scope=BlockScope.GLOBAL,
    ),
    MemoryBlockSchema(
        label="guidance",
        description="Active guidance for next session",
        retention_policy=RetentionPolicy.SHORT_TERM,
        merge_strategy=MergeStrategy.APPEND,
        injection_point=InjectionPoint.PRE_PROMPT,
        scope=BlockScope.SESSION,
    ),
    MemoryBlockSchema(
        label="pending_items",
        description="Unfinished work and TODOs",
        retention_policy=RetentionPolicy.SHORT_TERM,
        merge_strategy=MergeStrategy.APPEND,
        injection_point=InjectionPoint.PRE_PROMPT,
        scope=BlockScope.SESSION,
    ),
    MemoryBlockSchema(
        label="project_context",
        description="Codebase details and architectural decisions",
        retention_policy=RetentionPolicy.LONG_TERM,
        merge_strategy=MergeStrategy.APPEND,
        injection_point=InjectionPoint.PRE_PROMPT,
        scope=BlockScope.PROJECT,
    ),
    MemoryBlockSchema(
        label="session_patterns",
        description="Observed patterns across sessions",
        retention_policy=RetentionPolicy.LONG_TERM,
        merge_strategy=MergeStrategy.APPEND,
        injection_point=InjectionPoint.PRE_PROMPT,
        scope=BlockScope.SESSION,
    ),
    MemoryBlockSchema(
        label="user_preferences",
        description="Coding style, tool choices, communication preferences",
        retention_policy=RetentionPolicy.LONG_TERM,
        merge_strategy=MergeStrategy.APPEND,
        injection_point=InjectionPoint.PRE_PROMPT,
        scope=BlockScope.GLOBAL,
    ),
    MemoryBlockSchema(
        label="self_improvement",
        description="Memory architecture evolution procedures",
        retention_policy=RetentionPolicy.PERMANENT,
        merge_strategy=MergeStrategy.REPLACE,
        injection_point=InjectionPoint.WHISPER_ONLY,
        scope=BlockScope.GLOBAL,
    ),
    MemoryBlockSchema(
        label="tool_guidelines",
        description="Available tools and usage patterns",
        retention_policy=RetentionPolicy.PERMANENT,
        merge_strategy=MergeStrategy.REPLACE,
        injection_point=InjectionPoint.WHISPER_ONLY,
        scope=BlockScope.GLOBAL,
    ),
]


# =============================================================================
# Global Access Functions
# =============================================================================

def get_registry() -> BlockRegistry:
    """Get the global block registry instance."""
    return BlockRegistry()


def initialize_default_blocks() -> BlockRegistry:
    """Initialize registry with default block schemas."""
    registry = get_registry()
    for schema in DEFAULT_BLOCK_SCHEMAS:
        try:
            registry.register(schema)
        except ValueError:
            pass  # Already registered
    return registry
