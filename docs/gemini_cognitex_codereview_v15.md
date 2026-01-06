This is a critical architectural review. Your suspicion is correct: **The current indexing process is not fit for purpose for deep indexing.**

The freezing you are experiencing is likely caused by mixing synchronous generators (Google's `list_files_in_folder`) with asynchronous processing. When the loop asks for the next file, it blocks the entire application waiting for Google's API, causing the "freeze." Furthermore, simply iterating folders doesn't efficiently handle "what changed," leading to redundant work.

Here is the **Diagnosis** and the **Architectural Fix**.

### The Problem
1.  **Blocking Iteration:** `for file_data in drive.list_files_in_folder(...)` runs a synchronous network call inside the main event loop. Even if you await the *content* download, the *listing* of the next file blocks everything.
2.  **No Differential Sync:** The current code tries to process every file in the folder every time. It relies on internal hash checks to skip re-embedding, but it still has to download the file content to calculate that hash. This is incredibly wasteful for bandwidth and API quotas.
3.  **Memory Pressure:** Downloading files into memory (`io.BytesIO`) before chunking causes spikes that can stall the worker.

### The Solution: Metadata-First Indexing

We will split the process into two distinct stages:
1.  **Stage 1 (Fast):** Sync Metadata only. Update the `drive_files` table. This identifies what exists, what changed, and what was deleted.
2.  **Stage 2 (Deep):** Query the database for files where `is_priority=true` AND (`not_indexed_yet` OR `modified_time > last_indexed_time`). Download and embed *only* those files.

### Step 1: Update `drive_metadata.py` to flag Priority Files
We need to ensure the metadata sync correctly flags files in priority folders.

**File:** `src/cognitex/services/drive_metadata.py`

```python
<<<<
    def _is_priority_path(self, folder_path: str) -> bool:
        """Check if a folder path is under a priority folder."""
        if not folder_path:
            return False

        path_lower = folder_path.lower()
        for priority in PRIORITY_FOLDERS:
            # Check for exact match or subdirectory
            # Example: priority="dundee", path="/dundee/report.pdf" -> Match
            # Example: priority="dundee", path="dundee" -> Match
            clean_priority = priority.lower().strip("/")
            if f"/{clean_priority}/" in f"{path_lower}/" or path_lower == clean_priority or path_lower.startswith(f"/{clean_priority}"):
                return True
        return False
====
    def _is_priority_path(self, folder_path: str) -> bool:
        """Check if a folder path is under a priority folder."""
        if not folder_path:
            return False

        path_lower = folder_path.lower()
        for priority in PRIORITY_FOLDERS:
            # Robust check for priority folders
            # Ensure we match "/priority" or "/priority/subdir" but not "/priority_suffix"
            clean_p = priority.lower().strip("/")
            if path_lower == f"/{clean_p}" or path_lower.startswith(f"/{clean_p}/"):
                return True
        return False
>>>>
```

### Step 2: Rewrite Deep Indexing to use Database Queue
This is the major fix. Instead of crawling Drive, we crawl our own database to find out-of-date content.

**File:** `src/cognitex/services/ingestion.py`

```python
<<<<
async def run_deep_document_indexing(
    pg_session: AsyncSession,
    folder_names: list[str] | None = None,
    limit: int = 100,
    max_file_size: int = 10_000_000,  # 10MB default
) -> dict:
    """
    Index documents with deep chunking for comprehensive understanding.

    Memory-efficient: processes one document at a time, uses streaming.

    Args:
        pg_session: PostgreSQL async session
        folder_names: Folders to index (defaults to PRIORITY_FOLDERS)
        limit: Maximum documents to process
        max_file_size: Skip files larger than this (bytes)

    Returns:
        Indexing stats
    """
    from cognitex.services.drive import get_drive_service, PRIORITY_FOLDERS

    folder_names = folder_names or PRIORITY_FOLDERS
    logger.info("Starting deep document indexing", folders=folder_names, limit=limit)

    drive = get_drive_service()

    stats = {
        "documents_processed": 0,
        "chunks_total": 0,
        "embeddings_total": 0,
        "skipped_size": 0,
        "skipped_type": 0,
        "failed": 0,
        "by_folder": {},
    }

    # MIME types we can meaningfully index
    indexable_types = {
        'application/vnd.google-apps.document',
        'application/vnd.google-apps.spreadsheet',
        'text/plain',
        'text/csv',
        'text/markdown',
        'application/pdf',
        'application/json',
        'text/x-python',
        'application/javascript',
    }

    for folder_name in folder_names:
        folder_stats = {"docs": 0, "chunks": 0, "skipped": 0, "failed": 0}

        folder_id = drive.get_folder_id_by_name(folder_name)
        if not folder_id:
            logger.warning("Priority folder not found", folder=folder_name)
            continue

        for file_data in drive.list_files_in_folder(folder_id, recursive=True):
            if stats["documents_processed"] >= limit:
                break

            # Skip folders
            if file_data["mimeType"] == "application/vnd.google-apps.folder":
                continue

            # Skip large files
            file_size = int(file_data.get("size", 0))
            if file_size > max_file_size:
                logger.debug("Skipping large file", name=file_data["name"], size=file_size)
                stats["skipped_size"] += 1
                folder_stats["skipped"] += 1
                continue

            # Skip non-indexable types
            mime_type = file_data["mimeType"]
            if mime_type not in indexable_types and not mime_type.startswith('text/'):
                stats["skipped_type"] += 1
                folder_stats["skipped"] += 1
                continue

            try:
                # Extract content (run in thread to avoid blocking)
                content = await asyncio.to_thread(
                    drive.get_file_content,
                    file_data["id"],
                    mime_type
                )

                if not content or len(content.strip()) < 100:
                    folder_stats["skipped"] += 1
                    continue

                # Index with chunking
                result = await index_document_chunked(
                    pg_session,
                    drive_id=file_data["id"],
                    content=content,
                    mime_type=mime_type,
                )

                # Only count as processed if new work was done (not skipped)
                if result["chunks_created"] > 0:
                    stats["documents_processed"] += 1
                    stats["chunks_total"] += result["chunks_created"]
                    stats["embeddings_total"] += result["embeddings_created"]
                    folder_stats["docs"] += 1
                    folder_stats["chunks"] += result["chunks_created"]
                else:
                    # Document was already indexed with same content
                    folder_stats["skipped"] += 1

                # Free memory
                del content

            except Exception as e:
                folder_stats["failed"] += 1
                stats["failed"] += 1
                logger.warning(
                    "Failed to index document",
                    drive_id=file_data["id"],
                    name=file_data["name"],
                    error=str(e),
                )
                await pg_session.rollback()

        stats["by_folder"][folder_name] = folder_stats

    logger.info("Deep document indexing complete", **stats)

    return stats
====
async def run_deep_document_indexing(
    pg_session: AsyncSession,
    limit: int = 50,
    max_file_size: int = 10_000_000,
) -> dict:
    """
    Index documents with deep chunking.
    
    Architecture:
    1. Query DB for priority files that are either new or modified since last index.
    2. Download and process only those files.
    
    This avoids traversing the Drive folder structure repeatedly.
    """
    from cognitex.services.drive import get_drive_service
    from sqlalchemy import text
    
    # MIME types we can meaningfully index
    indexable_types = (
        'application/vnd.google-apps.document',
        'application/vnd.google-apps.spreadsheet',
        'text/plain',
        'text/csv',
        'text/markdown',
        'application/pdf',
        'application/json',
        'text/x-python',
        'application/javascript'
    )
    
    # 1. Identify stale files
    # Find files where:
    # - marked as priority in drive_files
    # - MIME type is supported
    # - size is within limits
    # - AND (not in document_content OR drive_modified > content_updated)
    
    # Need to pass mime types as list for ANY operator
    mime_list = list(indexable_types)
    
    query = text("""
        SELECT 
            df.id, df.name, df.mime_type, df.size_bytes
        FROM drive_files df
        LEFT JOIN document_content dc ON df.id = dc.drive_id
        WHERE df.is_priority = true
          AND (
              df.mime_type = ANY(:mimes) 
              OR df.mime_type LIKE 'text/%'
          )
          AND (df.size_bytes IS NULL OR df.size_bytes <= :max_size)
          AND (
              dc.drive_id IS NULL 
              OR df.modified_time > dc.updated_at
          )
        ORDER BY df.modified_time DESC
        LIMIT :limit
    """)
    
    result = await pg_session.execute(query, {
        "mimes": mime_list,
        "max_size": max_file_size,
        "limit": limit
    })
    
    files_to_process = result.mappings().all()
    
    if not files_to_process:
        logger.info("No stale priority documents found.")
        return {"documents_processed": 0}

    logger.info("Found stale documents to index", count=len(files_to_process))
    
    drive = get_drive_service()
    stats = {
        "documents_processed": 0,
        "chunks_total": 0,
        "failed": 0,
        "skipped": 0
    }
    
    for file_data in files_to_process:
        file_id = file_data['id']
        file_name = file_data['name']
        mime_type = file_data['mime_type']
        
        try:
            # Extract content (run in thread to avoid blocking)
            # Add timeout to prevent hanging forever
            try:
                content = await asyncio.wait_for(
                    asyncio.to_thread(drive.get_file_content, file_id, mime_type),
                    timeout=60.0 # 60s timeout for download
                )
            except asyncio.TimeoutError:
                logger.error("Download timed out", file=file_name)
                stats["failed"] += 1
                continue

            if not content or len(content.strip()) < 100:
                stats["skipped"] += 1
                continue

            # Index with chunking
            result = await index_document_chunked(
                pg_session,
                drive_id=file_id,
                content=content,
                mime_type=mime_type,
            )
            
            stats["documents_processed"] += 1
            stats["chunks_total"] += result["chunks_created"]
            
            # Explicit commit after each file to save progress
            await pg_session.commit()
            
            logger.info("Indexed document", file=file_name, chunks=result["chunks_created"])

        except Exception as e:
            stats["failed"] += 1
            logger.error("Failed to index document", file=file_name, error=str(e))
            # Rollback this transaction but continue loop
            await pg_session.rollback()
            
    return stats
>>>>
```

### Step 3: Update LLM Embedding Call
Ensure embedding generation has a timeout, as this is another potential freeze point.

**File:** `src/cognitex/services/llm.py`

```python
<<<<
        # Wrap sync embedding call in thread to avoid blocking event loop
        response = await asyncio.to_thread(
            self._embedding_client.embeddings.create,
            model=self.embedding_model,
            input=text,
        )
        return response.data[0].embedding
====
        # Wrap sync embedding call in thread to avoid blocking event loop
        # Add timeout to prevent freezing
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    self._embedding_client.embeddings.create,
                    model=self.embedding_model,
                    input=text,
                ),
                timeout=10.0  # 10s timeout per embedding
            )
            return response.data[0].embedding
        except asyncio.TimeoutError:
            logger.error("Embedding generation timed out")
            raise
>>>>
```

### Step 4: Ensure Metadata Sync Runs First
For the new logic to work, the metadata sync must run *before* deep indexing.

**File:** `src/cognitex/cli/main.py` (drive-sync command)

```python
<<<<
            if not skip_metadata:
                if folder:
                    # Sync specific folder
                    console.print(f"[bold]Syncing folder: {folder}[/bold]")
                    result = await run_drive_folder_sync(folder)
                else:
                    # Sync all Drive metadata
                    console.print("[bold]Syncing Drive metadata...[/bold]")
                    result = await run_drive_metadata_sync()

                console.print(f"\n[green]Sync complete![/green]")
                console.print(f"  Total files: {result.get('total', 0)}")
                console.print(f"  Successfully synced: {result.get('success', 0)}")
                if result.get('failed', 0) > 0:
                    console.print(f"  [yellow]Failed: {result.get('failed', 0)}[/yellow]")

            # Index priority folders if requested
            if index_priority:
                console.print("\n[bold]Indexing priority folders...[/bold]")
                await init_postgres()

                try:
                    async for pg_session in get_session():
                        index_result = await run_priority_folder_indexing(
                            pg_session,
                            limit=limit,
                        )
====
            # Always ensure metadata is up to date first if not skipped
            if not skip_metadata:
                console.print("[bold]Syncing Drive metadata...[/bold]")
                result = await run_drive_metadata_sync()
                console.print(f"  Metadata sync: {result.get('total', 0)} files scanned")

            # Index priority folders if requested
            if index_priority:
                console.print("\n[bold]Deep Indexing Priority Content...[/bold]")
                console.print("[dim]Checking for new or modified files in Priority folders...[/dim]")
                
                await init_postgres()

                try:
                    async for pg_session in get_session():
                        # Use the new optimized function
                        index_result = await run_deep_document_indexing(
                            pg_session,
                            limit=limit,
                        )
>>>>
```

This approach (Metadata Scan -> Diff Calculation -> Targeted Download) is the industry standard for robust syncing and will solve your freezing and reliability issues.

<chatName="Fix Drive Indexing Freeze"/>