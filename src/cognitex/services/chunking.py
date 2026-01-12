"""Document chunking for deep semantic understanding.

Splits documents into overlapping chunks suitable for embedding models
with limited context windows (512 tokens for bge-base-en-v1.5).
"""

import hashlib
import re
from dataclasses import dataclass

import structlog

logger = structlog.get_logger()

# Target ~300 tokens per chunk, with ~50 token overlap
# Assuming ~4 chars per token on average
CHUNK_SIZE = 1200  # chars (~300 tokens)
CHUNK_OVERLAP = 200  # chars (~50 tokens)
MIN_CHUNK_SIZE = 100  # Don't create tiny chunks


@dataclass
class DocumentChunk:
    """A chunk of a document with metadata."""
    content: str
    chunk_index: int
    start_char: int
    end_char: int
    content_hash: str

    @property
    def token_estimate(self) -> int:
        """Rough token count estimate."""
        return len(self.content) // 4


def compute_hash(text: str) -> str:
    """Compute SHA256 hash of text."""
    return hashlib.sha256(text.encode()).hexdigest()[:64]


def split_into_paragraphs(text: str) -> list[str]:
    """Split text into paragraphs, preserving structure."""
    # Split on multiple newlines (paragraph breaks)
    paragraphs = re.split(r'\n\s*\n', text)
    # Filter empty paragraphs
    return [p.strip() for p in paragraphs if p.strip()]


def chunk_document(
    content: str,
    chunk_size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> list[DocumentChunk]:
    """
    Split a document into overlapping chunks for embedding.

    Uses a paragraph-aware strategy:
    1. First try to split on paragraph boundaries
    2. If paragraphs are too long, split on sentences
    3. If sentences are too long, split on word boundaries

    Args:
        content: Full document text
        chunk_size: Target chunk size in characters
        overlap: Overlap between chunks in characters

    Returns:
        List of DocumentChunk objects
    """
    if not content or len(content.strip()) < MIN_CHUNK_SIZE:
        return []

    content = content.strip()
    chunks = []
    current_pos = 0
    chunk_index = 0
    prev_pos = -1  # Track previous position to detect infinite loops

    while current_pos < len(content):
        # Infinite loop detection - if position hasn't changed, force advance
        if current_pos == prev_pos:
            current_pos += chunk_size // 2  # Force forward progress
            continue
        prev_pos = current_pos

        # Calculate end position for this chunk
        end_pos = min(current_pos + chunk_size, len(content))

        # Try to find a good break point (paragraph, sentence, or word boundary)
        if end_pos < len(content):
            # Look for paragraph break first
            para_break = content.rfind('\n\n', current_pos, end_pos)
            if para_break > current_pos + MIN_CHUNK_SIZE:
                end_pos = para_break
            else:
                # Try sentence boundary (. ! ?)
                sentence_break = max(
                    content.rfind('. ', current_pos, end_pos),
                    content.rfind('! ', current_pos, end_pos),
                    content.rfind('? ', current_pos, end_pos),
                )
                if sentence_break > current_pos + MIN_CHUNK_SIZE:
                    end_pos = sentence_break + 1  # Include the period
                else:
                    # Fall back to word boundary
                    word_break = content.rfind(' ', current_pos, end_pos)
                    if word_break > current_pos + MIN_CHUNK_SIZE:
                        end_pos = word_break

        # Extract chunk text
        chunk_text = content[current_pos:end_pos].strip()

        if len(chunk_text) >= MIN_CHUNK_SIZE:
            chunks.append(DocumentChunk(
                content=chunk_text,
                chunk_index=chunk_index,
                start_char=current_pos,
                end_char=end_pos,
                content_hash=compute_hash(chunk_text),
            ))
            chunk_index += 1

        # Move position forward - ALWAYS ensure forward progress
        new_pos = end_pos - overlap
        # Ensure we move forward by at least 1 character
        if new_pos <= current_pos:
            new_pos = current_pos + 1
        current_pos = new_pos

    logger.debug(
        "Chunked document",
        total_chars=len(content),
        num_chunks=len(chunks),
        avg_chunk_size=len(content) // len(chunks) if chunks else 0,
    )

    return chunks


def chunk_csv_document(content: str, max_rows: int = 50) -> list[DocumentChunk]:
    """
    Special chunking for CSV files.

    CSVs are chunked by rows, keeping the header with each chunk.

    Args:
        content: CSV content
        max_rows: Maximum data rows per chunk

    Returns:
        List of DocumentChunk objects
    """
    lines = content.split('\n')
    if len(lines) < 2:
        return chunk_document(content)  # Fall back to regular chunking

    header = lines[0]
    data_lines = [l for l in lines[1:] if l.strip()]

    if not data_lines:
        return []

    chunks = []
    chunk_index = 0

    for i in range(0, len(data_lines), max_rows):
        chunk_lines = data_lines[i:i + max_rows]
        chunk_text = header + '\n' + '\n'.join(chunk_lines)

        start_char = len(header) + 1 + sum(len(l) + 1 for l in data_lines[:i])
        end_char = start_char + sum(len(l) + 1 for l in chunk_lines)

        chunks.append(DocumentChunk(
            content=chunk_text,
            chunk_index=chunk_index,
            start_char=start_char,
            end_char=end_char,
            content_hash=compute_hash(chunk_text),
        ))
        chunk_index += 1

    logger.debug(
        "Chunked CSV",
        total_rows=len(data_lines),
        num_chunks=len(chunks),
    )

    return chunks


def chunk_code_document(content: str) -> list[DocumentChunk]:
    """
    Special chunking for code files.

    Tries to split on function/class boundaries.

    Args:
        content: Code content

    Returns:
        List of DocumentChunk objects
    """
    # Patterns for code structure (Python, JS, etc.)
    patterns = [
        r'\n(?=def\s)',           # Python function
        r'\n(?=class\s)',         # Python class
        r'\n(?=function\s)',      # JS function
        r'\n(?=const\s+\w+\s*=)', # JS const assignment
        r'\n(?=export\s)',        # JS export
        r'\n(?=async\s+def\s)',   # Python async function
    ]

    # Combine patterns
    combined_pattern = '|'.join(patterns)

    # Split on code boundaries
    parts = re.split(combined_pattern, content)

    if len(parts) <= 1:
        # No code structure found, use regular chunking
        return chunk_document(content)

    chunks = []
    current_pos = 0
    chunk_index = 0

    for part in parts:
        part = part.strip()
        if not part:
            continue

        # If part is too large, sub-chunk it
        if len(part) > CHUNK_SIZE * 2:
            sub_chunks = chunk_document(part)
            for sub_chunk in sub_chunks:
                chunks.append(DocumentChunk(
                    content=sub_chunk.content,
                    chunk_index=chunk_index,
                    start_char=current_pos + sub_chunk.start_char,
                    end_char=current_pos + sub_chunk.end_char,
                    content_hash=sub_chunk.content_hash,
                ))
                chunk_index += 1
        elif len(part) >= MIN_CHUNK_SIZE:
            chunks.append(DocumentChunk(
                content=part,
                chunk_index=chunk_index,
                start_char=current_pos,
                end_char=current_pos + len(part),
                content_hash=compute_hash(part),
            ))
            chunk_index += 1

        current_pos += len(part) + 1  # +1 for the split newline

    logger.debug(
        "Chunked code",
        total_chars=len(content),
        num_chunks=len(chunks),
    )

    return chunks


def smart_chunk(content: str, mime_type: str | None = None) -> list[DocumentChunk]:
    """
    Intelligently chunk a document based on its type.

    Args:
        content: Document content
        mime_type: MIME type hint

    Returns:
        List of DocumentChunk objects
    """
    if not content:
        return []

    # Detect CSV
    if mime_type == 'text/csv' or (
        '\n' in content and
        ',' in content.split('\n')[0] and
        content.count(',') > content.count('\n')
    ):
        return chunk_csv_document(content)

    # Detect code
    code_indicators = ['def ', 'class ', 'function ', 'import ', 'const ', 'let ', 'var ']
    if mime_type in ('text/x-python', 'application/javascript', 'text/plain') or \
       any(ind in content[:1000] for ind in code_indicators):
        # Check if it really looks like code
        if content.count('def ') > 2 or content.count('function ') > 2:
            return chunk_code_document(content)

    # Default: regular chunking
    return chunk_document(content)


@dataclass
class EnhancedDocumentChunk(DocumentChunk):
    """A chunk with additional semantic metadata from document analysis."""
    section_title: str = ""  # What section this chunk belongs to
    chunk_type: str = ""  # Type of content: narrative, table, list, heading, action_items, etc.
    importance: float = 0.5  # 0-1 relevance score
    contains_decision: bool = False
    contains_action_item: bool = False
    contains_risk: bool = False
    entities: list[str] = None  # People, orgs, projects mentioned

    def __post_init__(self):
        if self.entities is None:
            self.entities = []


def chunk_with_sections(
    content: str,
    sections: list[dict],
    chunk_size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> list[EnhancedDocumentChunk]:
    """
    Chunk a document using section information from document analysis.

    Preserves section boundaries where possible, creating chunks that
    align with the document's semantic structure.

    Args:
        content: Full document text
        sections: List of {title, summary} dicts from document analysis
        chunk_size: Target chunk size in characters
        overlap: Overlap between chunks in characters

    Returns:
        List of EnhancedDocumentChunk objects with section attribution
    """
    if not content or not sections:
        # Fall back to regular chunking with enhanced wrapper
        basic_chunks = chunk_document(content, chunk_size, overlap)
        return [
            EnhancedDocumentChunk(
                content=c.content,
                chunk_index=c.chunk_index,
                start_char=c.start_char,
                end_char=c.end_char,
                content_hash=c.content_hash,
            )
            for c in basic_chunks
        ]

    chunks = []
    chunk_index = 0

    # Try to find section boundaries in the content
    remaining_content = content
    current_pos = 0

    for section in sections:
        section_title = section.get("title", "")

        if not section_title:
            continue

        # Try to find this section in the content
        # Search for the section title as a heading
        section_patterns = [
            f"\n{section_title}\n",
            f"\n{section_title}:",
            f"\n# {section_title}",
            f"\n## {section_title}",
            f"\n### {section_title}",
            section_title.upper(),
        ]

        section_start = -1
        for pattern in section_patterns:
            pos = content.find(pattern, current_pos)
            if pos != -1:
                section_start = pos
                break

        if section_start == -1:
            continue

        # Find section end (start of next section or end of document)
        section_end = len(content)
        for next_section in sections[sections.index(section) + 1:]:
            next_title = next_section.get("title", "")
            if next_title:
                for pattern in section_patterns:
                    pos = content.find(pattern.replace(section_title, next_title), section_start + len(section_title))
                    if pos != -1 and pos < section_end:
                        section_end = pos
                        break

        # Extract section content
        section_content = content[section_start:section_end].strip()

        if len(section_content) < MIN_CHUNK_SIZE:
            continue

        # Chunk this section
        if len(section_content) <= chunk_size:
            # Section fits in one chunk
            chunks.append(EnhancedDocumentChunk(
                content=section_content,
                chunk_index=chunk_index,
                start_char=section_start,
                end_char=section_end,
                content_hash=compute_hash(section_content),
                section_title=section_title,
            ))
            chunk_index += 1
        else:
            # Need to split section into multiple chunks
            section_chunks = chunk_document(section_content, chunk_size, overlap)
            for sc in section_chunks:
                chunks.append(EnhancedDocumentChunk(
                    content=sc.content,
                    chunk_index=chunk_index,
                    start_char=section_start + sc.start_char,
                    end_char=section_start + sc.end_char,
                    content_hash=sc.content_hash,
                    section_title=section_title,
                ))
                chunk_index += 1

        current_pos = section_end

    # If no sections were found or matched, fall back to regular chunking
    if not chunks:
        basic_chunks = chunk_document(content, chunk_size, overlap)
        return [
            EnhancedDocumentChunk(
                content=c.content,
                chunk_index=c.chunk_index,
                start_char=c.start_char,
                end_char=c.end_char,
                content_hash=c.content_hash,
            )
            for c in basic_chunks
        ]

    logger.debug(
        "Chunked with sections",
        total_chars=len(content),
        num_chunks=len(chunks),
        sections_found=len([c for c in chunks if c.section_title]),
    )

    return chunks


def annotate_chunks_with_analysis(
    chunks: list[DocumentChunk],
    analysis: dict,
) -> list[EnhancedDocumentChunk]:
    """
    Annotate chunks with information from document analysis.

    Marks chunks that contain decisions, action items, risks, etc.
    based on the document analysis results.

    Args:
        chunks: Basic document chunks
        analysis: Dict from DocumentAnalysis.to_dict()

    Returns:
        List of EnhancedDocumentChunk with annotations
    """
    # Extract keywords from analysis for matching
    decisions = analysis.get("key_decisions", [])
    action_items = analysis.get("action_items", [])
    risks = analysis.get("risks", [])
    key_entities = analysis.get("key_entities", {})
    sections = analysis.get("sections", [])

    # Build keyword sets for matching
    decision_keywords = set()
    for d in decisions:
        if isinstance(d, str):
            decision_keywords.update(d.lower().split()[:5])

    action_keywords = set()
    for a in action_items:
        if isinstance(a, dict):
            action_keywords.update(a.get("item", "").lower().split()[:5])
        elif isinstance(a, str):
            action_keywords.update(a.lower().split()[:5])

    risk_keywords = set()
    for r in risks:
        if isinstance(r, str):
            risk_keywords.update(r.lower().split()[:5])

    all_entities = []
    for entity_type, entities in key_entities.items():
        if isinstance(entities, list):
            all_entities.extend(entities)

    enhanced_chunks = []

    for chunk in chunks:
        content_lower = chunk.content.lower()

        # Check for decisions
        contains_decision = any(kw in content_lower for kw in decision_keywords if len(kw) > 3)

        # Check for action items
        contains_action = any(kw in content_lower for kw in action_keywords if len(kw) > 3)

        # Check for risks
        contains_risk = any(kw in content_lower for kw in risk_keywords if len(kw) > 3)

        # Find entities in chunk
        chunk_entities = [e for e in all_entities if e.lower() in content_lower]

        # Determine chunk type
        chunk_type = "narrative"
        if "|" in chunk.content and chunk.content.count("|") > 3:
            chunk_type = "table"
        elif chunk.content.count("\n- ") > 2 or chunk.content.count("\n* ") > 2:
            chunk_type = "list"
        elif chunk.content.count("\n") < 2 and len(chunk.content) < 200:
            chunk_type = "heading"

        # Calculate importance score
        importance = 0.5
        if contains_decision:
            importance += 0.2
        if contains_action:
            importance += 0.2
        if contains_risk:
            importance += 0.1
        if len(chunk_entities) > 0:
            importance += 0.1 * min(len(chunk_entities), 3)
        importance = min(importance, 1.0)

        # Find section title for this chunk
        section_title = ""
        for section in sections:
            title = section.get("title", "")
            if title and title.lower() in content_lower[:200]:
                section_title = title
                break

        enhanced_chunks.append(EnhancedDocumentChunk(
            content=chunk.content,
            chunk_index=chunk.chunk_index,
            start_char=chunk.start_char,
            end_char=chunk.end_char,
            content_hash=chunk.content_hash,
            section_title=section_title,
            chunk_type=chunk_type,
            importance=importance,
            contains_decision=contains_decision,
            contains_action_item=contains_action,
            contains_risk=contains_risk,
            entities=chunk_entities,
        ))

    logger.debug(
        "Annotated chunks",
        total_chunks=len(enhanced_chunks),
        with_decisions=sum(1 for c in enhanced_chunks if c.contains_decision),
        with_actions=sum(1 for c in enhanced_chunks if c.contains_action_item),
        with_risks=sum(1 for c in enhanced_chunks if c.contains_risk),
    )

    return enhanced_chunks
