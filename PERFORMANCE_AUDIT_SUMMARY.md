# Performance Audit Summary

## Key Findings

**CRITICAL:**
- graph_store.py:554-586 - Graph traversal edge lookup with potential O(n²) complexity (mitigated by batching)

**HIGH:**
- projections/reports.py:101-110 - UserActivityReport: nested loop causing O(n log n) per user
- websocket/server.py:303-307 - Inefficient fixed-interval cleanup loop (every minute)
- hybrid_retriever.py:347-427 - TF-IDF search recomputes IDF/tokenization per query
- projections/builder.py:102, 199 - Double iteration over reports in build_all()

**MEDIUM:**
- entity_extractor.py - Likely uncompiled regex patterns causing recompilation overhead
- rate_limiter.py - Frequent time.time() calls in token bucket algorithm

## Verified Performant Components
- connection_pool.py - Thread-safe with proper stale connection detection
- sql_helpers.py - Parameterized queries and identifier validation
- Migration scripts - Proper transactions and batch operations
- WebSocket server - Efficient event buffering and connection management

## Recommendations
1. Implement caching for TF-IDF vectors and graph traversal results
2. Replace nested loops with single-pass algorithms (sort once, group after)
3. Optimize polling mechanisms with dynamic timing/priority queues
4. Pre-compile regex patterns and optimize time function usage
5. Consider connection pool tuning based on workload patterns

Full report: PERFORMANCE_AUDIT_REPORT.md