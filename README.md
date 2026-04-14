# Foresight MCP Server

**Persistent memory for AI agents with psychological safety features and subconscious memory blocks.**

Restored from `src/lib/ai/memory/` and `ai/memory/hindsight_subconscious.py` - the heart and soul of Foresight.

Compatible with Claude Code, Goose, Cursor, and any MCP-compatible AI agent.

## Architecture

Foresight implements a sophisticated memory system with:

### Core Memory System
- **Socratic Gate** - Psychological safety gating for memory ingestion
- **Crisis Detection** - Automatic detection of crisis signals with risk assessment
- **Memory Synthesizer** - Reconciliation and stance shift detection
- **Memory Linker** - Vector store linking and ghost node archival
- **Rich Memory Types** - Emotional metadata, empathy metrics, retention policies

### Subconscious Memory Blocks
- **Guidance** - Active guidance for next session
- **Pending Items** - Unfinished work and TODOs
- **Project Context** - Codebase details and architectural decisions
- **Session Patterns** - Observed patterns across sessions
- **User Preferences** - Coding style, tool choices, communication preferences
- **Self Improvement** - Memory architecture evolution and learning procedures
- **Tool Guidelines** - Available tools and usage patterns

## Quick Start

```bash
uv run foresight-mcp
```

## Add to Your MCP Client

### Claude Code

```json
{
  "mcpServers": {
    "foresight": {
      "command": "uv",
      "args": ["run", "-m", "foresight_mcp"],
      "cwd": "/path/to/foresight-mcp",
      "env": {
        "FORESIGHT_DB_PATH": "/home/user/.foresight/memory.db",
        "FORESIGHT_USER_ID": "username"
      }
    }
  }
}
```

### Goose

Add to your Goose configuration (`~/.config/goose/config.yaml`):

```yaml
extensions:
  foresight:
    args: ["run", "-m", "foresight_mcp"]
    cwd: /path/to/foresight-mcp
    env:
      FORESIGHT_DB_PATH: /home/user/.foresight/memory.db
      FORESIGHT_USER_ID: username
    type: stdio
```

### Cursor / Other MCP Clients

Use the same configuration pattern as Claude Code, adjusting for your client's specific config format.

## Tools

### Core Memory Operations

- `store_memory` - Store memory with emotional context and metrics
- `query_memories` - Search memories by content
- `list_memories` - List all memories for a user
- `get_memory` - Get full memory details
- `update_memory` - Update memory content/metadata
- `delete_memory` - Delete a memory
- `memory_status` - System status

### Advanced Memory Features

- `synthesize_memories` - Run synthesis to detect stance shifts and merge candidates
- `archive_memory` - Archive memory to ghost node (requires vector_id)

### Subconscious Memory Blocks

- `get_subconscious_blocks` - Get all subconscious memory blocks
- `get_subconscious_block` - Get a specific block (guidance, pending_items, etc.)
- `update_subconscious_block` - Update a block's content
- `add_subconscious_guidance` - Add a line to the guidance block
- `get_subconscious_whisper` - Get the current whisper injection (XML format)
- `get_subconscious_context` - Get all blocks as XML context
- `reset_subconscious_block` - Reset a block to default
- `clear_subconscious_block` - Clear a block's content
- `process_session_transcript` - Process session transcript and extract memories

## Memory Scopes

- **session** - Relevant only to the current conversation
- **arc** - Spans multiple sessions in a single training arc
- **trait** - Permanent modification to persona traits
- **fact** - Objective fact discovered about the user/trainee

## Retention Policies

- **ephemeral** - Deleted after the session
- **short_term** - Kept for the duration of the arc
- **long_term** - Candidate for Ghost Node archival
- **permanent** - Never archived

## Psychological Safety Features

### Crisis Detection

Automatically detects crisis signals including:
- Self-harm ideation
- Depression indicators
- Anxiety/panic episodes
- Trauma responses
- Substance abuse
- Eating disorders
- Crisis events

### Gate Decisions

- **auto** - Normal information flow
- **passive** - Flagged for review in post-session summary
- **active** - Requires supervisor confirmation
- **block** - Blocked for safety

## Example Usage

### Store Memory with Emotional Context

```python
# Store memory with emotional context
store_memory(
    content="User expressed feeling hopeless about their progress",
    scope="session",
    retention="short_term",
    emotional_context={
        "valence": -0.6,
        "arousal": 0.4,
        "dominance": 0.2,
        "primary_emotion": "sadness",
        "intensity": 0.7
    },
    metrics={
        "reciprocity": 0.5,
        "validation_accuracy": 0.8,
        "resistance_level": 0.3
    }
)
```

### Process Session Transcript

```python
# Process a session transcript to extract preferences and patterns
process_session_transcript(
    session_id="session-123",
    messages=[
        {"role": "user", "content": "I always prefer snake_case for function names"},
        {"role": "assistant", "content": "Understood. I'll use snake_case."},
    ],
    project_path="/path/to/project"
)
```

### Get Subconscious Guidance

```python
# Get the current whisper (guidance for next session)
whisper = get_subconscious_whisper()

# Get all memory blocks as XML
context = get_subconscious_context()

# Add guidance line
add_subconscious_guidance("User prefers concise responses without filler.")
```

### Run Synthesis

```python
# Run synthesis to detect stance shifts
synthesize_memories()
```

## Subconscious Memory Block Labels

- `core_directives` - Role definition and operating principles
- `guidance` - Active guidance for next session
- `pending_items` - Unfinished work and TODOs
- `project_context` - Codebase details and architectural decisions
- `session_patterns` - Observed patterns across sessions
- `user_preferences` - Coding style, tool choices, communication preferences
- `self_improvement` - Memory architecture evolution procedures
- `tool_guidelines` - Available tools and usage patterns

## License

MIT
