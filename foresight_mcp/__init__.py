"""
Foresight MCP Server - Full memory system with psychological safety features.
Restored from src/lib/ai/memory/ architecture.

Includes:
- MemoryObject with emotional context and empathy metrics
- Socratic Gate for psychological safety
- Anomaly Detection (mental health, extensible to other domains)
- Memory Synthesizer for reconciliation and stance shift detection
- Memory Linker for vector store and ghost nodes
- Composable memory block schemas with BlockRegistry
- Subconscious memory blocks (guidance, pending_items, preferences, patterns)
- Event bus with persistence and audit trail
- Event hook system for extensibility (HTTP webhooks, callables, async)
- Memory versioning with rollback capabilities
- Multi-tenant isolation with rate limiting
# - Compliance exporters for HIPAA, SOC2, GDPR -- Removed
"""

from typing import TYPE_CHECKING, Any

# Core system exports
from .block_registry import (
    BlockRegistry as BlockRegistry,
    BlockScope as BlockScope,
    InjectionPoint as InjectionPoint,
    MemoryBlock as MemoryBlock,
    MemoryBlockSchema as MemoryBlockSchema,
    MergeStrategy as MergeStrategy,
    RetentionPolicy as RetentionPolicy,
    get_registry as get_registry,
    initialize_default_blocks as initialize_default_blocks,
)
from .context_blocks import (
    ContextBlock as ContextBlock,
    ContextBlockAgent as ContextBlockAgent,
    ContextBlockState as ContextBlockState,
    add_context_guidance as add_context_guidance,
    add_subconscious_guidance as add_subconscious_guidance,
    clear_context_block as clear_context_block,
    clear_subconscious_block as clear_subconscious_block,
    get_context_block as get_context_block,
    get_context_block_agent as get_context_block_agent,
    get_context_snapshot as get_context_snapshot,
    get_context_whisper as get_context_whisper,
    get_subconscious_block as get_subconscious_block,
    get_subconscious_context as get_subconscious_context,
    get_subconscious_whisper as get_subconscious_whisper,
    list_context_blocks as list_context_blocks,
    reset_context_block as reset_context_block,
    reset_subconscious_block as reset_subconscious_block,
    update_context_block as update_context_block,
    update_subconscious_block as update_subconscious_block,
)
from .enhanced_synthesizer import (
    Contradiction as Contradiction,
    EnhancedMemorySynthesizer as EnhancedMemorySynthesizer,
    EnhancedSynthesisResult as EnhancedSynthesisResult,
    Insight as Insight,
    TemporalTrend as TemporalTrend,
    get_enhanced_synthesizer as get_enhanced_synthesizer,
    reset_enhanced_synthesizer as reset_enhanced_synthesizer,
)
from .entity_extractor import (
    Entity as Entity,
    EntityExtractor as EntityExtractor,
    ExtractionResult as ExtractionResult,
    Relationship as Relationship,
    get_entity_extractor as get_entity_extractor,
    reset_entity_extractor as reset_entity_extractor,
)
from .event_bus import (
    Event as Event,
    EventBus as EventBus,
    EventType as EventType,
    get_event_bus as get_event_bus,
)
from .profile_synthesizer import (
    ProfileConfig as ProfileConfig,
    profile_to_prompt as profile_to_prompt,
    synthesize_profile as synthesize_profile,
)
from .graph_store import (
    GraphStore as GraphStore,
    GraphTraversalResult as GraphTraversalResult,
    get_graph_store as get_graph_store,
    reset_graph_store as reset_graph_store,
)
from .hooks import (
    HookExecutor as HookExecutor,
    HookRegistration as HookRegistration,
    HookRegistry as HookRegistry,
    HookType as HookType,
    get_hook_executor as get_hook_executor,
    list_hooks as list_hooks,
    register_hook as register_hook,
    unregister_hook as unregister_hook,
)
from .hybrid_retriever import (
    HybridResult as HybridResult,
    HybridRetriever as HybridRetriever,
    HybridSearchResult as HybridSearchResult,
    get_hybrid_retriever as get_hybrid_retriever,
    reset_hybrid_retriever as reset_hybrid_retriever,
)
from .reflection_engine import (
    ReflectionEngine as ReflectionEngine,
    ReflectionInsight as ReflectionInsight,
    ReflectionReport as ReflectionReport,
    get_reflection_engine as get_reflection_engine,
    reset_reflection_engine as reset_reflection_engine,
)
from .server import (
    AnalysisAction as AnalysisAction,
    ContextBlockAction as ContextBlockAction,
    CurationRunAction as CurationRunAction,
    EntityAction as EntityAction,
    EntityQuery as EntityQuery,
    MemoryAction as MemoryAction,
    MemoryOptions as MemoryOptions,
    MemoryUpdateOptions as MemoryUpdateOptions,
    SearchOptions as SearchOptions,
    SubconsciousAction as SubconsciousAction,
    SystemStatusOptions as SystemStatusOptions,
    TemporalWindow as TemporalWindow,
    VersionAction as VersionAction,
    analyze_memories as analyze_memories,
    archive_memory as archive_memory,
    delete_memory as delete_memory,
    get_memory as get_memory,
    get_system_status as get_system_status,
    inject_context as inject_context,
    list_memories as list_memories,
    manage_context_blocks as manage_context_blocks,
    manage_curation_runs as manage_curation_runs,
    manage_entities as manage_entities,
    manage_memories as manage_memories,
    manage_memory_versions as manage_memory_versions,
    manage_subconscious as manage_subconscious,
    mcp as mcp,
    memory_status as memory_status,
    process_session_transcript as process_session_transcript,
    query_entities as query_entities,
    query_memories as query_memories,
    query_memories_temporal as query_memories_temporal,
    search_memories as search_memories,
    store_memory as store_memory,
    switch_tenant as switch_tenant,
    update_memory as update_memory,
)
from .temporal_queries import (
    TemporalQueryBuilder as TemporalQueryBuilder,
    TemporalQueryResult as TemporalQueryResult,
    TimeWindow as TimeWindow,
    get_temporal_query_builder as get_temporal_query_builder,
    reset_temporal_query_builder as reset_temporal_query_builder,
)
from .temporal_schema import (
    initialize_decay_config as initialize_decay_config,
    run_temporal_migrations as run_temporal_migrations,
)
from .temporal_service import (
    DecayConfig as DecayConfig,
    FreshnessTrend as FreshnessTrend,
    TemporalService as TemporalService,
    get_temporal_service as get_temporal_service,
    reset_temporal_service as reset_temporal_service,
)

# Optional stream producer and consumer dependencies
if TYPE_CHECKING:
    from .consumer_group import (
        ConsumerRecord as ConsumerRecord,
        ConsumerState as ConsumerState,
        ConsumerStats as ConsumerStats,
        KafkaConsumerGroup as KafkaConsumerGroup,
    )
    from .stream_producer import (
        KafkaProducer as KafkaProducer,
        KinesisProducer as KinesisProducer,
        MockProducer as MockProducer,
        StreamEvent as StreamEvent,
        StreamProducer as StreamProducer,
        StreamPublisher as StreamPublisher,
        StreamType as StreamType,
        create_stream_producer as create_stream_producer,
    )

    _stream_producer_available = True
    _consumer_group_available = True
else:
    # Stream producer (optional)
    try:
        from .stream_producer import (
            KafkaProducer,
            KinesisProducer,
            MockProducer,
            StreamEvent,
            StreamProducer,
            StreamPublisher,
            StreamType,
            create_stream_producer,
        )

        _stream_producer_available = True
    except ImportError:
        _stream_producer_available = False

        class _OptionalStreamDependencyStub:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                raise ImportError(
                    f"{self.__class__.__name__} requires kafka-python or boto3. "
                    "Install with: pip install kafka-python boto3"
                )

        StreamProducer = StreamPublisher = StreamEvent = StreamType = _OptionalStreamDependencyStub
        KafkaProducer = KinesisProducer = MockProducer = _OptionalStreamDependencyStub

        def create_stream_producer(*_args: Any, **_kwargs: Any) -> Any:
            raise ImportError(
                "create_stream_producer requires kafka-python or boto3. Install with: pip install kafka-python boto3"
            )

    # Consumer group (optional)
    try:
        from .consumer_group import (
            ConsumerRecord,
            ConsumerState,
            ConsumerStats,
            KafkaConsumerGroup,
        )

        _consumer_group_available = True
    except ImportError:
        _consumer_group_available = False

        class _OptionalConsumerDependencyStub:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                raise ImportError(
                    f"{self.__class__.__name__} requires kafka-python. Install with: pip install kafka-python"
                )

        KafkaConsumerGroup = ConsumerRecord = ConsumerStats = ConsumerState = _OptionalConsumerDependencyStub
# CRDT and Sync exports
from .crdt import (
    LWWMap as LWWMap,
    LWWRegister as LWWRegister,
    ORSet as ORSet,
    VectorClock as VectorClock,
)
from .sync import (
    Operation as Operation,
    OperationQueue as OperationQueue,
    OperationType as OperationType,
    SyncManager as SyncManager,
    SyncProgress as SyncProgress,
    SyncStatus as SyncStatus,
    get_sync_manager as get_sync_manager,
    reset_sync_manager as reset_sync_manager,
)
from .websocket.server import (
    Connection as Connection,
    ConnectionState as ConnectionState,
    WebSocketHandler as WebSocketHandler,
    WebSocketServer as WebSocketServer,
)
from .websocket.subscriptions import (
    Subscription as Subscription,
    SubscriptionManager as SubscriptionManager,
    get_subscription_manager as get_subscription_manager,
    reset_subscription_manager as reset_subscription_manager,
)

__version__ = "1.2.0"
__all__ = [
    "AnalysisAction",
    "BlockRegistry",
    "BlockScope",
    "Connection",
    "ConnectionState",
    "ConsumerRecord",
    "ConsumerState",
    "ConsumerStats",
    "ContextBlock",
    "ContextBlockAction",
    "ContextBlockAgent",
    "ContextBlockState",
    "Contradiction",
    "CurationRunAction",
    "DecayConfig",
    "EnhancedMemorySynthesizer",
    "EnhancedSynthesisResult",
    "Entity",
    "EntityAction",
    "EntityExtractor",
    "EntityQuery",
    "Event",
    "EventBus",
    "EventType",
    "ExtractionResult",
    "FreshnessTrend",
    "GraphStore",
    "GraphTraversalResult",
    "HookExecutor",
    "HookRegistration",
    "HookRegistry",
    "HookType",
    "HybridResult",
    "HybridRetriever",
    "HybridSearchResult",
    "InjectionPoint",
    "Insight",
    "KafkaConsumerGroup",
    "KafkaProducer",
    "KinesisProducer",
    "LWWMap",
    "LWWRegister",
    "MemoryAction",
    "MemoryBlock",
    "MemoryBlockSchema",
    "MemoryOptions",
    "MemoryUpdateOptions",
    "MergeStrategy",
    "MockProducer",
    "ORSet",
    "Operation",
    "OperationQueue",
    "OperationType",
    "ReflectionEngine",
    "ReflectionInsight",
    "ReflectionReport",
    "Relationship",
    "RetentionPolicy",
    "SearchOptions",
    "StreamEvent",
    "StreamProducer",
    "StreamPublisher",
    "StreamType",
    "SubconsciousAction",
    "Subscription",
    "SubscriptionManager",
    "SyncManager",
    "SyncProgress",
    "SyncStatus",
    "SystemStatusOptions",
    "TemporalQueryBuilder",
    "TemporalQueryResult",
    "TemporalService",
    "TemporalTrend",
    "TemporalWindow",
    "TimeWindow",
    "VectorClock",
    "VersionAction",
    "WebSocketHandler",
    "WebSocketServer",
    "add_context_guidance",
    "add_subconscious_guidance",
    "analyze_memories",
    "archive_memory",
    "clear_context_block",
    "clear_subconscious_block",
    "create_stream_producer",
    "delete_memory",
    "get_context_block",
    "get_context_block_agent",
    "get_context_snapshot",
    "get_context_whisper",
    "get_enhanced_synthesizer",
    "get_entity_extractor",
    "get_event_bus",
    "get_graph_store",
    "get_hook_executor",
    "get_hybrid_retriever",
    "get_memory",
    "get_reflection_engine",
    "get_registry",
    "get_subconscious_block",
    "get_subconscious_context",
    "get_subconscious_whisper",
    "get_system_status",
    "get_temporal_query_builder",
    "get_temporal_service",
    "initialize_decay_config",
    "initialize_default_blocks",
    "inject_context",
    "list_context_blocks",
    "list_hooks",
    "list_memories",
    "manage_context_blocks",
    "manage_curation_runs",
    "manage_entities",
    "manage_memories",
    "manage_memory_versions",
    "manage_subconscious",
    "mcp",
    "memory_status",
    "process_session_transcript",
    "profile_to_prompt",
    "query_entities",
    "query_memories",
    "query_memories_temporal",
    "register_hook",
    "reset_context_block",
    "reset_enhanced_synthesizer",
    "reset_entity_extractor",
    "reset_graph_store",
    "reset_hybrid_retriever",
    "reset_reflection_engine",
    "reset_subconscious_block",
    "reset_sync_manager",
    "reset_temporal_query_builder",
    "reset_temporal_service",
    "run_temporal_migrations",
    "search_memories",
    "store_memory",
    "switch_tenant",
    "synthesize_profile",
    "unregister_hook",
    "update_context_block",
    "update_memory",
    "update_subconscious_block",
]
