"""Semantic analysis service using Gemini 2 Flash.

Analyzes priority folder documents to extract:
- Summary
- Key concepts and topics
- Named entities (people, organizations, technologies)
- Cross-document associations
"""

import asyncio
import gc
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import google.generativeai as genai
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cognitex.config import get_settings
from cognitex.db.postgres import get_session
from cognitex.db.neo4j import get_driver
from cognitex.services.drive import get_drive_service, EXPORTABLE_MIME_TYPES

logger = structlog.get_logger()

# Rate limiting: Gemini 2.0 Flash Exp has 10 RPM limit
REQUESTS_PER_MINUTE = 10
MIN_REQUEST_INTERVAL = 60.0 / REQUESTS_PER_MINUTE  # 6 seconds between requests


# SQL statements for creating the document_analysis table
CREATE_TABLE_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS document_analysis (
        file_id VARCHAR(255) PRIMARY KEY,
        summary TEXT,
        key_concepts JSONB,
        topics JSONB,
        entities JSONB,
        analyzed_at TIMESTAMP DEFAULT NOW(),
        llm_model VARCHAR(100)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_document_analysis_analyzed_at ON document_analysis(analyzed_at)",
    """
    CREATE TABLE IF NOT EXISTS analysis_errors (
        id SERIAL PRIMARY KEY,
        file_id VARCHAR(255) NOT NULL,
        error_type VARCHAR(50) NOT NULL,
        error_message TEXT,
        raw_response TEXT,
        created_at TIMESTAMP DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_analysis_errors_file_id ON analysis_errors(file_id)",
]


@dataclass
class ScoredItem:
    """An extracted item with a relevance/confidence score."""
    name: str
    score: float  # 0.0-1.0
    is_primary: bool = False  # For topics: is this a main topic?
    context: str | None = None  # Optional context snippet


@dataclass
class DocumentAnalysis:
    """Result of semantic analysis for a document."""
    file_id: str
    summary: str
    key_concepts: list[ScoredItem]  # Concepts with confidence scores
    topics: list[ScoredItem]  # Topics with relevance scores
    entities: dict[str, list[str]]  # {"people": [], "organizations": [], "technologies": []}


ANALYSIS_PROMPT = """Analyze this document and extract semantic information. Return ONLY valid JSON with no other text.

For topics and concepts, include a relevance score (0.0-1.0) indicating how central they are to the document.
Mark primary topics (the 1-3 main themes) with is_primary: true.

{
    "summary": "A 2-3 sentence summary of the document's purpose and main content",
    "key_concepts": [
        {"name": "concept name", "score": 0.9, "context": "brief phrase showing usage"},
        {"name": "another concept", "score": 0.7}
    ],
    "topics": [
        {"name": "main topic", "score": 0.95, "is_primary": true},
        {"name": "secondary topic", "score": 0.6, "is_primary": false}
    ],
    "entities": {
        "people": ["names of people mentioned"],
        "organizations": ["companies, institutions, groups"],
        "technologies": ["specific tools, platforms, languages, frameworks"]
    }
}

Guidelines for scores:
- 0.9-1.0: Central to the document, extensively discussed
- 0.7-0.9: Important, mentioned multiple times with substance
- 0.5-0.7: Relevant but not a main focus
- 0.3-0.5: Tangentially mentioned
- <0.3: Brief mention, likely not worth extracting

Document content:
"""


async def ensure_tables() -> None:
    """Create the document_analysis table if it doesn't exist."""
    async for session in get_session():
        for stmt in CREATE_TABLE_STATEMENTS:
            await session.execute(text(stmt))
        await session.commit()
        logger.info("document_analysis table ensured")


class SemanticAnalyzer:
    """Service for deep semantic analysis of documents using Gemini."""

    def __init__(self):
        settings = get_settings()
        genai.configure(api_key=settings.google_ai_api_key.get_secret_value())
        self.model = genai.GenerativeModel('gemini-2.0-flash-exp')
        self.drive = get_drive_service()
        self._last_request_time = 0.0

    async def _rate_limit(self) -> None:
        """Enforce rate limiting between API calls."""
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < MIN_REQUEST_INTERVAL:
            wait_time = MIN_REQUEST_INTERVAL - elapsed
            logger.debug("Rate limiting", wait_seconds=round(wait_time, 1))
            await asyncio.sleep(wait_time)
        self._last_request_time = time.time()

    async def _call_gemini_with_retry(self, prompt: str, max_retries: int = 3) -> str | None:
        """Call Gemini API with rate limiting and retry on 429 errors."""
        for attempt in range(max_retries):
            await self._rate_limit()

            try:
                response = self.model.generate_content(
                    prompt,
                    generation_config=genai.GenerationConfig(
                        temperature=0.2,
                        max_output_tokens=2000,
                    )
                )
                return response.text.strip()

            except Exception as e:
                error_str = str(e)
                if "429" in error_str or "quota" in error_str.lower():
                    # Extract retry delay from error message if available
                    retry_match = re.search(r'retry_delay.*?seconds:\s*(\d+)', error_str)
                    wait_time = int(retry_match.group(1)) + 5 if retry_match else 60

                    if attempt < max_retries - 1:
                        logger.warning(
                            "Rate limited by Gemini, waiting to retry",
                            attempt=attempt + 1,
                            wait_seconds=wait_time
                        )
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        logger.error("Rate limited, max retries exceeded")
                        raise
                else:
                    # Non-rate-limit error, don't retry
                    raise

        return None

    async def analyze_document(self, file_id: str, content: str) -> DocumentAnalysis | None:
        """
        Analyze a single document with Gemini.

        Args:
            file_id: The Google Drive file ID
            content: The document text content

        Returns:
            DocumentAnalysis or None if analysis failed
        """
        if not content or len(content.strip()) < 100:
            logger.warning("Document too short for analysis", file_id=file_id, length=len(content))
            return None

        # Truncate very long documents to fit context
        max_chars = 500000  # ~125k tokens, well under 1M limit
        if len(content) > max_chars:
            content = content[:max_chars] + "\n\n[Document truncated for analysis]"

        response_text = None
        try:
            prompt = ANALYSIS_PROMPT + content

            response_text = await self._call_gemini_with_retry(prompt)
            if not response_text:
                return None

            # Try to extract JSON from response
            json_match = re.search(r'\{[\s\S]*\}', response_text)
            if json_match:
                data = json.loads(json_match.group())
            else:
                data = json.loads(response_text)

            # Parse concepts - handle both new scored format and legacy string list
            raw_concepts = data.get("key_concepts", [])
            concepts = []
            for item in raw_concepts:
                if isinstance(item, dict):
                    concepts.append(ScoredItem(
                        name=item.get("name", ""),
                        score=float(item.get("score", 0.8)),
                        context=item.get("context"),
                    ))
                elif isinstance(item, str):
                    # Legacy format - assign default score
                    concepts.append(ScoredItem(name=item, score=0.8))

            # Parse topics - handle both new scored format and legacy string list
            raw_topics = data.get("topics", [])
            topics = []
            for item in raw_topics:
                if isinstance(item, dict):
                    topics.append(ScoredItem(
                        name=item.get("name", ""),
                        score=float(item.get("score", 0.8)),
                        is_primary=item.get("is_primary", False),
                    ))
                elif isinstance(item, str):
                    # Legacy format - assign default score
                    topics.append(ScoredItem(name=item, score=0.8))

            return DocumentAnalysis(
                file_id=file_id,
                summary=data.get("summary", ""),
                key_concepts=concepts,
                topics=topics,
                entities=data.get("entities", {}),
            )

        except json.JSONDecodeError as e:
            logger.error("Failed to parse Gemini response as JSON", file_id=file_id, error=str(e))
            await self._log_error(file_id, "json_parse", str(e), response_text)
            return None
        except Exception as e:
            error_str = str(e)
            # Don't log rate limit errors to error table (they'll be retried)
            if "429" not in error_str and "quota" not in error_str.lower():
                logger.error("Gemini analysis failed", file_id=file_id, error=error_str)
                await self._log_error(file_id, "gemini_error", error_str, None)
            else:
                logger.error("Gemini quota exceeded after retries", file_id=file_id)
            return None

    async def _log_error(self, file_id: str, error_type: str, error_message: str, raw_response: str | None) -> None:
        """Log an analysis error to the database for later retry."""
        async for session in get_session():
            await session.execute(
                text("""
                INSERT INTO analysis_errors (file_id, error_type, error_message, raw_response)
                VALUES (:file_id, :error_type, :error_message, :raw_response)
                """),
                {
                    "file_id": file_id,
                    "error_type": error_type,
                    "error_message": error_message[:500] if error_message else None,
                    "raw_response": raw_response[:5000] if raw_response else None,
                }
            )
            await session.commit()
            logger.info("Logged analysis error for retry", file_id=file_id, error_type=error_type)

    async def save_analysis(self, analysis: DocumentAnalysis) -> None:
        """Save analysis results to database and graph."""
        # Save to PostgreSQL
        async for session in get_session():
            await session.execute(
                text("""
                INSERT INTO document_analysis (file_id, summary, key_concepts, topics, entities, analyzed_at, llm_model)
                VALUES (:file_id, :summary, :key_concepts, :topics, :entities, NOW(), :llm_model)
                ON CONFLICT (file_id) DO UPDATE SET
                    summary = EXCLUDED.summary,
                    key_concepts = EXCLUDED.key_concepts,
                    topics = EXCLUDED.topics,
                    entities = EXCLUDED.entities,
                    analyzed_at = NOW(),
                    llm_model = EXCLUDED.llm_model
                """),
                {
                    "file_id": analysis.file_id,
                    "summary": analysis.summary,
                    "key_concepts": json.dumps([asdict(c) for c in analysis.key_concepts]),
                    "topics": json.dumps([asdict(t) for t in analysis.topics]),
                    "entities": json.dumps(analysis.entities),
                    "llm_model": "gemini-2.0-flash-exp",
                }
            )
            await session.commit()

        # Update Neo4j graph
        await self._update_graph(analysis)

    async def _update_graph(self, analysis: DocumentAnalysis) -> None:
        """Create Concept/Topic nodes and relationships in Neo4j."""
        driver = get_driver()

        async with driver.session() as session:
            # Create/update concepts and link to document
            for concept in analysis.key_concepts:
                concept_name = concept.name if isinstance(concept, ScoredItem) else str(concept)
                if not concept_name or len(concept_name.strip()) < 2:
                    continue
                concept_normalized = concept_name.strip().lower()
                await session.run("""
                    MERGE (c:Concept {name: $name})
                    ON CREATE SET c.created_at = datetime()
                    WITH c
                    MATCH (d:Document {drive_id: $drive_id})
                    MERGE (d)-[:ABOUT]->(c)
                """, name=concept_normalized, drive_id=analysis.file_id)

            # Create/update topics and link to document
            for topic in analysis.topics:
                topic_name = topic.name if isinstance(topic, ScoredItem) else str(topic)
                if not topic_name or len(topic_name.strip()) < 2:
                    continue
                topic_normalized = topic_name.strip().lower()
                await session.run("""
                    MERGE (t:Topic {name: $name})
                    ON CREATE SET t.created_at = datetime()
                    WITH t
                    MATCH (d:Document {drive_id: $drive_id})
                    MERGE (d)-[:COVERS]->(t)
                """, name=topic_normalized, drive_id=analysis.file_id)

            # Link to Person nodes for mentioned people
            for person_name in analysis.entities.get("people", []):
                if not person_name or len(person_name.strip()) < 2:
                    continue
                # Try to find existing person by name
                await session.run("""
                    MATCH (d:Document {drive_id: $drive_id})
                    MATCH (p:Person)
                    WHERE toLower(p.name) CONTAINS toLower($person_name)
                    MERGE (d)-[:MENTIONS]->(p)
                """, drive_id=analysis.file_id, person_name=person_name.strip())

            # Update document with summary
            await session.run("""
                MATCH (d:Document {drive_id: $drive_id})
                SET d.summary = $summary,
                    d.analyzed_at = datetime()
            """, drive_id=analysis.file_id, summary=analysis.summary)

    async def analyze_priority_files(
        self,
        folder: str | None = None,
        limit: int | None = None,
        skip_analyzed: bool = True,
        max_file_size_mb: int = 5,
    ) -> dict:
        """
        Analyze all priority folder documents.

        Args:
            folder: Optional specific folder name to analyze
            limit: Optional limit on files to analyze
            skip_analyzed: Skip files already analyzed
            max_file_size_mb: Skip files larger than this (default 5MB)

        Returns:
            Stats dict with counts
        """
        await ensure_tables()

        stats = {"total": 0, "analyzed": 0, "skipped": 0, "skipped_size": 0, "errors": 0}
        max_size_bytes = max_file_size_mb * 1024 * 1024

        # Get priority files from drive_files table
        async for session in get_session():
            query = """
                SELECT df.id, df.name, df.mime_type, df.folder_path
                FROM drive_files df
                WHERE df.is_priority = true
            """
            params = {}

            if folder:
                query += " AND LOWER(df.folder_path) LIKE :folder_pattern"
                params["folder_pattern"] = f"%/{folder.lower()}%"

            if skip_analyzed:
                query += " AND df.id NOT IN (SELECT file_id FROM document_analysis)"

            # Filter to text-extractable types
            extractable = list(EXPORTABLE_MIME_TYPES.keys()) + [
                "text/plain", "text/markdown", "text/csv", "application/pdf",
                "application/json", "text/x-python",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ]
            placeholders = ", ".join(f":mime_{i}" for i in range(len(extractable)))
            query += f" AND df.mime_type IN ({placeholders})"
            for i, mt in enumerate(extractable):
                params[f"mime_{i}"] = mt

            query += " ORDER BY df.modified_time DESC"

            if limit:
                query += f" LIMIT {limit}"

            result = await session.execute(text(query), params)
            files = result.mappings().all()

        logger.info(f"Found {len(files)} priority files to analyze")

        for file in files:
            stats["total"] += 1
            content = None

            try:
                # Check file size BEFORE loading content to avoid OOM
                try:
                    metadata = self.drive.get_file_metadata(file["id"])
                    file_size = int(metadata.get("size") or 0)
                    if file_size > max_size_bytes:
                        logger.info("Skipping large file", name=file["name"], size_mb=file_size / 1024 / 1024)
                        stats["skipped_size"] += 1
                        continue
                except Exception:
                    pass  # If metadata fails, try to get content anyway

                # Get file content (run in thread to avoid blocking)
                content = await asyncio.to_thread(
                    self.drive.get_file_content,
                    file["id"],
                    file["mime_type"]
                )
                if not content:
                    logger.warning("Could not extract content", file_id=file["id"], name=file["name"])
                    stats["skipped"] += 1
                    continue

                # Analyze with Gemini
                analysis = await self.analyze_document(file["id"], content)
                if not analysis:
                    stats["errors"] += 1
                    continue

                # Save results
                await self.save_analysis(analysis)
                stats["analyzed"] += 1

                logger.info(
                    "Analyzed document",
                    name=file["name"],
                    concepts=len(analysis.key_concepts),
                    topics=len(analysis.topics),
                )

            except Exception as e:
                logger.error("Failed to analyze file", file_id=file["id"], error=str(e))
                stats["errors"] += 1

            finally:
                # Free memory after each file to prevent OOM
                del content
                gc.collect()

        logger.info("Semantic analysis complete", **stats)
        return stats

    async def get_stats(self) -> dict:
        """Get analysis statistics."""
        async for session in get_session():
            result = await session.execute(text("""
                SELECT
                    COUNT(*) as analyzed_count,
                    COUNT(DISTINCT jsonb_array_elements_text(key_concepts)) as unique_concepts,
                    COUNT(DISTINCT jsonb_array_elements_text(topics)) as unique_topics
                FROM document_analysis
            """))
            row = result.mappings().one_or_none()
            if row:
                return dict(row)
        return {}

    async def get_analysis(self, file_id: str) -> dict | None:
        """Get analysis for a specific file."""
        async for session in get_session():
            result = await session.execute(
                text("SELECT * FROM document_analysis WHERE file_id = :file_id"),
                {"file_id": file_id}
            )
            row = result.mappings().one_or_none()
            return dict(row) if row else None
        return None
