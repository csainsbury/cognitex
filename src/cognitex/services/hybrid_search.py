"""Hybrid search combining vector similarity and keyword matching.

Provides unified search across documents, tasks, and emails by combining:
- Semantic search (vector embeddings for conceptual similarity)
- Keyword search (PostgreSQL tsvector for exact matches)

The hybrid approach gives the best of both worlds:
- Semantic catches concepts even with different wording
- Keyword catches exact terms, names, and technical jargon
"""

from dataclasses import dataclass
from enum import Enum

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()


class SearchMode(str, Enum):
    """Search mode for hybrid search."""
    SEMANTIC_ONLY = "semantic"
    KEYWORD_ONLY = "keyword"
    HYBRID = "hybrid"


@dataclass
class SearchResult:
    """A search result with scoring breakdown."""
    entity_type: str  # 'document', 'task', 'email', 'chunk'
    entity_id: str
    title: str
    content: str
    semantic_score: float = 0.0
    keyword_score: float = 0.0
    combined_score: float = 0.0
    metadata: dict | None = None


async def hybrid_search_documents(
    query: str,
    limit: int = 20,
    mode: SearchMode = SearchMode.HYBRID,
    semantic_weight: float = 0.6,
    keyword_weight: float = 0.4,
) -> list[SearchResult]:
    """
    Search documents using hybrid semantic + keyword matching.

    Args:
        query: Search query text
        limit: Maximum results to return
        mode: Search mode (semantic, keyword, or hybrid)
        semantic_weight: Weight for semantic scores in hybrid mode (0-1)
        keyword_weight: Weight for keyword scores in hybrid mode (0-1)

    Returns:
        List of SearchResult objects sorted by combined score
    """
    from cognitex.db.postgres import get_session
    from cognitex.services.llm import get_llm_service

    results = []

    async for session in get_session():
        if mode == SearchMode.KEYWORD_ONLY:
            results = await _keyword_search_documents(session, query, limit)
        elif mode == SearchMode.SEMANTIC_ONLY:
            results = await _semantic_search_documents(session, query, limit)
        else:
            # Hybrid: run both searches and combine
            semantic_results = await _semantic_search_documents(session, query, limit * 2)
            keyword_results = await _keyword_search_documents(session, query, limit * 2)

            # Merge results by entity_id
            results = _merge_search_results(
                semantic_results,
                keyword_results,
                semantic_weight,
                keyword_weight,
                limit,
            )
        break

    return results


async def hybrid_search_chunks(
    query: str,
    limit: int = 20,
    mode: SearchMode = SearchMode.HYBRID,
    semantic_weight: float = 0.6,
    keyword_weight: float = 0.4,
) -> list[SearchResult]:
    """
    Search document chunks using hybrid semantic + keyword matching.

    Useful for finding specific passages within large documents.
    """
    from cognitex.db.postgres import get_session

    results = []

    async for session in get_session():
        if mode == SearchMode.KEYWORD_ONLY:
            results = await _keyword_search_chunks(session, query, limit)
        elif mode == SearchMode.SEMANTIC_ONLY:
            results = await _semantic_search_chunks(session, query, limit)
        else:
            semantic_results = await _semantic_search_chunks(session, query, limit * 2)
            keyword_results = await _keyword_search_chunks(session, query, limit * 2)

            results = _merge_search_results(
                semantic_results,
                keyword_results,
                semantic_weight,
                keyword_weight,
                limit,
            )
        break

    return results


async def hybrid_search_all(
    query: str,
    limit: int = 20,
    include_documents: bool = True,
    include_chunks: bool = True,
    include_code: bool = False,
    mode: SearchMode = SearchMode.HYBRID,
) -> list[SearchResult]:
    """
    Search across all entity types with hybrid matching.

    Args:
        query: Search query text
        limit: Maximum results per entity type
        include_documents: Include document content
        include_chunks: Include document chunks
        include_code: Include code files
        mode: Search mode

    Returns:
        Merged list of SearchResult objects from all sources
    """
    all_results = []

    if include_documents:
        doc_results = await hybrid_search_documents(query, limit, mode)
        all_results.extend(doc_results)

    if include_chunks:
        chunk_results = await hybrid_search_chunks(query, limit, mode)
        all_results.extend(chunk_results)

    if include_code:
        code_results = await _search_code_hybrid(query, limit, mode)
        all_results.extend(code_results)

    # Sort by combined score and take top N
    all_results.sort(key=lambda x: x.combined_score, reverse=True)
    return all_results[:limit]


# =============================================================================
# Internal search implementations
# =============================================================================

async def _semantic_search_documents(
    session: AsyncSession,
    query: str,
    limit: int,
) -> list[SearchResult]:
    """Semantic search using vector embeddings."""
    from cognitex.services.llm import get_llm_service

    try:
        llm = get_llm_service()
        query_embedding = await llm.generate_embedding(query)
        query_embedding_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

        result = await session.execute(text("""
            SELECT
                dc.drive_id,
                dc.content,
                df.name as title,
                df.folder_path,
                1 - (e.embedding <=> CAST(:query_embedding AS vector)) as similarity
            FROM embeddings e
            JOIN document_content dc ON dc.drive_id = e.entity_id
            LEFT JOIN drive_files df ON df.id = dc.drive_id
            WHERE e.entity_type = 'document'
            ORDER BY e.embedding <=> CAST(:query_embedding AS vector)
            LIMIT :limit
        """), {
            "query_embedding": query_embedding_str,
            "limit": limit,
        })

        results = []
        for row in result.fetchall():
            results.append(SearchResult(
                entity_type="document",
                entity_id=row.drive_id,
                title=row.title or "Untitled",
                content=row.content[:500] if row.content else "",
                semantic_score=float(row.similarity),
                combined_score=float(row.similarity),
                metadata={"folder_path": row.folder_path},
            ))
        return results

    except Exception as e:
        logger.warning("Semantic document search failed", error=str(e))
        return []


async def _keyword_search_documents(
    session: AsyncSession,
    query: str,
    limit: int,
) -> list[SearchResult]:
    """Keyword search using PostgreSQL tsvector."""
    try:
        # Use plainto_tsquery for simpler query parsing
        result = await session.execute(text("""
            SELECT
                dc.drive_id,
                dc.content,
                df.name as title,
                df.folder_path,
                ts_rank_cd(to_tsvector('english', dc.content), plainto_tsquery('english', :query)) as rank
            FROM document_content dc
            LEFT JOIN drive_files df ON df.id = dc.drive_id
            WHERE to_tsvector('english', dc.content) @@ plainto_tsquery('english', :query)
            ORDER BY rank DESC
            LIMIT :limit
        """), {
            "query": query,
            "limit": limit,
        })

        results = []
        max_rank = 0.0
        rows = result.fetchall()

        # Find max rank for normalization
        for row in rows:
            if row.rank > max_rank:
                max_rank = row.rank

        for row in rows:
            # Normalize rank to 0-1 scale
            normalized_rank = row.rank / max_rank if max_rank > 0 else 0.0
            results.append(SearchResult(
                entity_type="document",
                entity_id=row.drive_id,
                title=row.title or "Untitled",
                content=row.content[:500] if row.content else "",
                keyword_score=normalized_rank,
                combined_score=normalized_rank,
                metadata={"folder_path": row.folder_path},
            ))
        return results

    except Exception as e:
        logger.warning("Keyword document search failed", error=str(e))
        return []


async def _semantic_search_chunks(
    session: AsyncSession,
    query: str,
    limit: int,
) -> list[SearchResult]:
    """Semantic search on document chunks."""
    from cognitex.services.llm import get_llm_service

    try:
        llm = get_llm_service()
        query_embedding = await llm.generate_embedding(query)
        query_embedding_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

        result = await session.execute(text("""
            SELECT
                dc.drive_id,
                dc.chunk_index,
                dc.content,
                dc.start_char,
                dc.end_char,
                df.name as doc_title,
                1 - (e.embedding <=> CAST(:query_embedding AS vector)) as similarity
            FROM embeddings e
            JOIN document_chunks dc ON dc.drive_id || ':' || dc.chunk_index = e.entity_id
            LEFT JOIN drive_files df ON df.id = dc.drive_id
            WHERE e.entity_type = 'chunk'
            ORDER BY e.embedding <=> CAST(:query_embedding AS vector)
            LIMIT :limit
        """), {
            "query_embedding": query_embedding_str,
            "limit": limit,
        })

        results = []
        for row in result.fetchall():
            results.append(SearchResult(
                entity_type="chunk",
                entity_id=f"{row.drive_id}:{row.chunk_index}",
                title=f"{row.doc_title or 'Document'} (chunk {row.chunk_index})",
                content=row.content[:500] if row.content else "",
                semantic_score=float(row.similarity),
                combined_score=float(row.similarity),
                metadata={
                    "drive_id": row.drive_id,
                    "chunk_index": row.chunk_index,
                    "start_char": row.start_char,
                    "end_char": row.end_char,
                },
            ))
        return results

    except Exception as e:
        logger.warning("Semantic chunk search failed", error=str(e))
        return []


async def _keyword_search_chunks(
    session: AsyncSession,
    query: str,
    limit: int,
) -> list[SearchResult]:
    """Keyword search on document chunks."""
    try:
        result = await session.execute(text("""
            SELECT
                dc.drive_id,
                dc.chunk_index,
                dc.content,
                dc.start_char,
                dc.end_char,
                df.name as doc_title,
                ts_rank_cd(to_tsvector('english', dc.content), plainto_tsquery('english', :query)) as rank
            FROM document_chunks dc
            LEFT JOIN drive_files df ON df.id = dc.drive_id
            WHERE to_tsvector('english', dc.content) @@ plainto_tsquery('english', :query)
            ORDER BY rank DESC
            LIMIT :limit
        """), {
            "query": query,
            "limit": limit,
        })

        results = []
        max_rank = 0.0
        rows = result.fetchall()

        for row in rows:
            if row.rank > max_rank:
                max_rank = row.rank

        for row in rows:
            normalized_rank = row.rank / max_rank if max_rank > 0 else 0.0
            results.append(SearchResult(
                entity_type="chunk",
                entity_id=f"{row.drive_id}:{row.chunk_index}",
                title=f"{row.doc_title or 'Document'} (chunk {row.chunk_index})",
                content=row.content[:500] if row.content else "",
                keyword_score=normalized_rank,
                combined_score=normalized_rank,
                metadata={
                    "drive_id": row.drive_id,
                    "chunk_index": row.chunk_index,
                    "start_char": row.start_char,
                    "end_char": row.end_char,
                },
            ))
        return results

    except Exception as e:
        logger.warning("Keyword chunk search failed", error=str(e))
        return []


async def _search_code_hybrid(
    query: str,
    limit: int,
    mode: SearchMode,
) -> list[SearchResult]:
    """Search code files with hybrid matching."""
    from cognitex.db.postgres import get_session
    from cognitex.services.llm import get_llm_service

    results = []

    async for session in get_session():
        try:
            if mode in (SearchMode.SEMANTIC_ONLY, SearchMode.HYBRID):
                # Semantic search on code
                llm = get_llm_service()
                query_embedding = await llm.generate_embedding(query)
                query_embedding_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

                semantic_result = await session.execute(text("""
                    SELECT
                        cc.file_id,
                        cc.path,
                        cc.repo_name,
                        cc.content,
                        1 - (e.embedding <=> CAST(:query_embedding AS vector)) as similarity
                    FROM embeddings e
                    JOIN code_content cc ON cc.file_id = e.entity_id
                    WHERE e.entity_type = 'code'
                    ORDER BY e.embedding <=> CAST(:query_embedding AS vector)
                    LIMIT :limit
                """), {
                    "query_embedding": query_embedding_str,
                    "limit": limit,
                })

                for row in semantic_result.fetchall():
                    results.append(SearchResult(
                        entity_type="code",
                        entity_id=row.file_id,
                        title=row.path,
                        content=row.content[:500] if row.content else "",
                        semantic_score=float(row.similarity),
                        combined_score=float(row.similarity),
                        metadata={"repo_name": row.repo_name},
                    ))

            if mode in (SearchMode.KEYWORD_ONLY, SearchMode.HYBRID) and not results:
                # Keyword fallback for code
                keyword_result = await session.execute(text("""
                    SELECT
                        cc.file_id,
                        cc.path,
                        cc.repo_name,
                        cc.content,
                        ts_rank_cd(to_tsvector('english', cc.content), plainto_tsquery('english', :query)) as rank
                    FROM code_content cc
                    WHERE to_tsvector('english', cc.content) @@ plainto_tsquery('english', :query)
                    ORDER BY rank DESC
                    LIMIT :limit
                """), {
                    "query": query,
                    "limit": limit,
                })

                for row in keyword_result.fetchall():
                    results.append(SearchResult(
                        entity_type="code",
                        entity_id=row.file_id,
                        title=row.path,
                        content=row.content[:500] if row.content else "",
                        keyword_score=1.0,  # Simplified scoring
                        combined_score=1.0,
                        metadata={"repo_name": row.repo_name},
                    ))

        except Exception as e:
            logger.warning("Code search failed", error=str(e))

        break

    return results


def _merge_search_results(
    semantic_results: list[SearchResult],
    keyword_results: list[SearchResult],
    semantic_weight: float,
    keyword_weight: float,
    limit: int,
) -> list[SearchResult]:
    """Merge and score results from semantic and keyword searches."""
    # Index results by entity_id
    merged: dict[str, SearchResult] = {}

    # Add semantic results
    for result in semantic_results:
        key = f"{result.entity_type}:{result.entity_id}"
        merged[key] = result

    # Merge keyword results
    for result in keyword_results:
        key = f"{result.entity_type}:{result.entity_id}"
        if key in merged:
            # Combine scores
            merged[key].keyword_score = result.keyword_score
        else:
            merged[key] = result

    # Calculate combined scores
    for result in merged.values():
        result.combined_score = (
            result.semantic_score * semantic_weight +
            result.keyword_score * keyword_weight
        )

    # Sort by combined score and return top N
    sorted_results = sorted(merged.values(), key=lambda x: x.combined_score, reverse=True)
    return sorted_results[:limit]


# =============================================================================
# Convenience functions
# =============================================================================

async def search(query: str, limit: int = 20) -> list[SearchResult]:
    """
    Simple hybrid search across all content.

    This is the main entry point for general search queries.
    """
    return await hybrid_search_all(
        query=query,
        limit=limit,
        include_documents=True,
        include_chunks=True,
        include_code=False,
        mode=SearchMode.HYBRID,
    )


async def search_with_context(
    query: str,
    context: str | None = None,
    limit: int = 10,
) -> list[SearchResult]:
    """
    Search with optional context to refine results.

    Args:
        query: Main search query
        context: Optional context (e.g., project name, topic)
        limit: Maximum results

    Returns:
        Search results, potentially filtered by context
    """
    # Combine query with context if provided
    full_query = f"{query} {context}" if context else query

    results = await search(full_query, limit * 2)

    # If context provided, boost results that mention it
    if context:
        context_lower = context.lower()
        for result in results:
            if context_lower in result.content.lower() or context_lower in result.title.lower():
                result.combined_score *= 1.2  # 20% boost

        results.sort(key=lambda x: x.combined_score, reverse=True)

    return results[:limit]
