from foresight_mcp.schema import (
    MemoryCreateOptions,
    MemoryScope,
    RetentionPolicy,
    SourceService,
    EmotionalContext,
    EmpathyMetrics,
)


def test_memory_create_options_defaults():
    options = MemoryCreateOptions()
    assert options.tenant_id == "default"
    assert options.bank_id == "default"
    assert options.scope == MemoryScope.SESSION
    assert options.retention == RetentionPolicy.SHORT_TERM
    assert options.category == "general"
    assert options.tags is None
    assert options.importance == 0.5
    assert options.source_service == SourceService.FORESIGHT
    assert options.emotional_context is None
    assert options.empathy_metrics is None
    assert options.relation_type is None
    assert options.related_memory_id is None


def test_memory_create_options_custom_values():
    options = MemoryCreateOptions(
        tenant_id="custom_tenant",
        bank_id="custom_bank",
        scope=MemoryScope.FACT,
        retention=RetentionPolicy.PERMANENT,
        category="custom_category",
        tags=["tag1", "tag2"],
        importance=0.9,
        source_service=SourceService.AI_SERVICES,
        emotional_context=EmotionalContext(
            valence=0.5,
            arousal=0.5,
            dominance=0.5,
            primary_emotion="joy",
            intensity=0.5,
        ),
        empathy_metrics=EmpathyMetrics(
            reciprocity=0.8, validation_accuracy=0.8, resistance_level=0.1
        ),
        relation_type="supports",
        related_memory_id="12345",
    )
    assert options.tenant_id == "custom_tenant"
    assert options.bank_id == "custom_bank"
    assert options.scope == MemoryScope.FACT
    assert options.retention == RetentionPolicy.PERMANENT
    assert options.category == "custom_category"
    assert options.tags == ["tag1", "tag2"]
    assert options.importance == 0.9
    assert options.source_service == SourceService.AI_SERVICES
    assert options.emotional_context.primary_emotion == "joy"
    assert options.empathy_metrics.reciprocity == 0.8
    assert options.relation_type == "supports"
    assert options.related_memory_id == "12345"
