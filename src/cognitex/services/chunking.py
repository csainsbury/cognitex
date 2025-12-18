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

    while current_pos < len(content):
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

        # Move position, accounting for overlap
        current_pos = end_pos - overlap
        if current_pos <= chunks[-1].start_char if chunks else 0:
            # Prevent infinite loop if overlap is too large
            current_pos = end_pos

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
