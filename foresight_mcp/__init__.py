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
- Compliance exporters for HIPAA, SOC2, GDPR
"""
from .server import (
    mcp,
    store_memory,
    query_memories,
    list_memories,
    get_memory,
    update_memory,
    delete_memory,
    memory_status,
    synthesize_memories,
    archive_memory,
    # Versioning tools
    get_memory_versions,
    rollback_memory,
    diff_memories,
    # Multi-tenant isolation
    create_tenant,
    get_tenant,
    list_tenants,
    update_tenant_config,
    switch_tenant,
    get_tenant_isolation_status,
    # Compliance exporters
    compliance_hipaa_access_log,
    compliance_hipaa_modification_log,
    compliance_hipaa_user_activity,
    compliance_soc2_change_history,
    compliance_soc2_access_review,
    compliance_soc2_monitoring,
    compliance_gdpr_data_export,
    compliance_gdpr_erasure_certification,
    compliance_save_report,
    # Subconscious tools
    get_subconscious_blocks,
    get_subconscious_block,
    update_subconscious_block,
    add_subconscious_guidance,
    get_subconscious_whisper,
    get_subconscious_context,
    reset_subconscious_block,
    clear_subconscious_block,
    process_session_transcript,
    # Audit tools
    audit_build,
    audit_list_reports,
    audit_export,
    audit_summary,
)
# Temporal memory exports
from .temporal_service import (
    TemporalService,
    DecayConfig,
    FreshnessTrend,
    get_temporal_service,
    reset_temporal_service,
)
from .temporal_queries import (
    TemporalQueryBuilder,
    TemporalQueryResult,
    TimeWindow,
    get_temporal_query_builder,
    reset_temporal_query_builder,
)
from .temporal_schema import (
    run_temporal_migrations,
    initialize_decay_config,
)
# Entity and graph exports
from .entity_extractor import (
    EntityExtractor,
    Entity,
    Relationship,
    ExtractionResult,
    get_entity_extractor,
    reset_entity_extractor,
)
from .graph_store import (
    GraphStore,
    GraphTraversalResult,
    get_graph_store,
    reset_graph_store,
)
# Block registry exports
from .block_registry import (
    BlockRegistry,
    MemoryBlockSchema,
    MemoryBlock,
    RetentionPolicy,
    MergeStrategy,
    InjectionPoint,
    BlockScope,
    get_registry,
    initialize_default_blocks,
)
# Event bus exports
from .event_bus import (
    get_event_bus,
    EventBus,
    Event,
    EventType,
)

# Hook system exports
from .hooks import (
    get_hook_executor,
    HookExecutor,
    HookRegistry,
    HookRegistration,
    HookType,
    list_hooks,
    register_hook,
    unregister_hook,
)

# Stream producer exports (optional - may not have kafka-python installed)
try:
    from .stream_producer import (
        StreamProducer,
        StreamPublisher,
        StreamEvent,
        StreamType,
        KafkaProducer,
        KinesisProducer,
        MockProducer,
        create_stream_producer,
    )
    _stream_producer_available = True
except ImportError:
    _stream_producer_available = False
    # Create stub classes for when kafka-python is not installed
    class StreamProducer: pass  # type: ignore
    class StreamPublisher: pass  # type: ignore
    class StreamEvent: pass  # type: ignore
    class StreamType: pass  # type: ignore
    class KafkaProducer: pass  # type: ignore
    class KinesisProducer: pass  # type: ignore
    class MockProducer: pass  # type: ignore
    def create_stream_producer(*args, **kwargs):  # type: ignore
        raise ImportError("kafka-python or boto3 not installed")

# Consumer group exports (optional - may not have kafka-python installed)
try:
    from .consumer_group import (
        KafkaConsumerGroup,
        ConsumerRecord,
        ConsumerStats,
        ConsumerState,
    )
    _consumer_group_available = True
except ImportError:
    _consumer_group_available = False
    # Create stub classes
    class KafkaConsumerGroup: pass  # type: ignore
    class ConsumerRecord: pass  # type: ignore
    class ConsumerStats: pass  # type: ignore
    class ConsumerState: pass  # type: ignore
# Hook system exports
from .hooks import (
    get_hook_executor,
    HookExecutor,
    HookRegistry,
    HookRegistration,
    HookType,
    list_hooks,
    register_hook,
    unregister_hook,
)
# WebSocket exports
from .websocket.subscriptions import (
    SubscriptionManager,
    Subscription,
    get_subscription_manager,
    reset_subscription_manager,
)
from .websocket.server import (
    WebSocketServer,
    WebSocketHandler,
    ConnectionState,
    Connection,
)
# CRDT exports
from .crdt import (
    VectorClock,
    LWWRegister,
    ORSet,
    LWWMap,
)
# Sync exports
from .sync import (
    SyncManager,
    SyncStatus,
    OperationType,
    Operation,
    OperationQueue,
    SyncProgress,
    get_sync_manager,
    reset_sync_manager,
)
# Projections exports
from .projections.builder import ProjectionBuilder
from .projections.reports import (
    MemoryTimeline,
    UserActivityReport,
    BlockChangeLog,
    AccessLog,
    AnomalyReport,
)

__version__ = "1.2.0"
__all__ = [
    "mcp",
    "store_memory",
    "query_memories",
    "list_memories",
    "get_memory",
    "update_memory",
    "delete_memory",
    "memory_status",
    "synthesize_memories",
    "archive_memory",
    # Versioning tools
    "get_memory_versions",
    "rollback_memory",
    "diff_memories",
    # Multi-tenant isolation
    "create_tenant",
    "get_tenant",
    "list_tenants",
    "update_tenant_config",
    "switch_tenant",
    "get_tenant_isolation_status",
    # Compliance exporters
    "compliance_hipaa_access_log",
    "compliance_hipaa_modification_log",
    "compliance_hipaa_user_activity",
    "compliance_soc2_change_history",
    "compliance_soc2_access_review",
    "compliance_soc2_monitoring",
    "compliance_gdpr_data_export",
    "compliance_gdpr_erasure_certification",
    "compliance_save_report",
    # Subconscious
    "get_subconscious_blocks",
    "get_subconscious_block",
    "update_subconscious_block",
    "add_subconscious_guidance",
    "get_subconscious_whisper",
    "get_subconscious_context",
    "reset_subconscious_block",
    "clear_subconscious_block",
    "process_session_transcript",
    # Audit tools
    "audit_build",
    "audit_list_reports",
    "audit_export",
    "audit_summary",
    # Block registry
    "BlockRegistry",
    "MemoryBlockSchema",
    "MemoryBlock",
    "RetentionPolicy",
    "MergeStrategy",
    "InjectionPoint",
    "BlockScope",
    "get_registry",
    "initialize_default_blocks",
    # Stream processing
    "StreamProducer",
    "StreamPublisher",
    "StreamEvent",
    "StreamType",
    "KafkaProducer",
    "KinesisProducer",
    "MockProducer",
    "create_stream_producer",
    # Consumer group
    "KafkaConsumerGroup",
    "ConsumerRecord",
    "ConsumerStats",
    "ConsumerState",
    # WebSocket
    "WebSocketServer",
    "WebSocketHandler",
    "ConnectionState",
    "Connection",
    "SubscriptionManager",
    "Subscription",
    "get_subscription_manager",
    "reset_subscription_manager",
    # CRDT
    "VectorClock",
    "LWWRegister",
    "ORSet",
    "LWWMap",
    # Sync
    "SyncManager",
    "SyncStatus",
    "OperationType",
    "Operation",
    "OperationQueue",
    "SyncProgress",
    "get_sync_manager",
    "reset_sync_manager",
]
