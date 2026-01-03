"""Core sync functionality for cognitex-sync."""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx

from .config import SyncConfig, load_config, load_state, save_state


class SessionDiscovery:
    """Discover coding sessions from CLI tools."""

    def __init__(self, config: SyncConfig):
        self.config = config

    def discover_sessions(self, cli_type: str = "claude") -> list[dict]:
        """Discover available coding sessions from a CLI tool.

        Args:
            cli_type: Which CLI to scan (default: claude)

        Returns:
            List of session metadata dicts
        """
        sessions = []
        base_path = self.config.cli_paths.get(cli_type)

        if not base_path:
            return sessions

        base_path = Path(base_path)
        if not base_path.exists():
            return sessions

        # Each subdirectory is a project (path-encoded)
        for project_dir in base_path.iterdir():
            if not project_dir.is_dir():
                continue

            # Decode project path from directory name
            project_path = "/" + project_dir.name.replace("-", "/")

            # Find session files
            for session_file in project_dir.glob("*.jsonl"):
                stat = session_file.stat()
                sessions.append({
                    "cli_type": cli_type,
                    "session_file": str(session_file),
                    "session_id": session_file.stem,
                    "project_path": project_path,
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "modified_ts": stat.st_mtime,
                })

        return sessions

    def parse_session_file(self, session_file: str, max_messages: int = 50) -> dict:
        """Parse a session JSONL file.

        Args:
            session_file: Path to the JSONL session file
            max_messages: Maximum messages to include (for bandwidth)

        Returns:
            Dict with session metadata and messages
        """
        path = Path(session_file)
        if not path.exists():
            return {}

        messages = []
        metadata = {}

        try:
            with open(path, "r") as f:
                for i, line in enumerate(f):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)

                        # Extract metadata from first message
                        if i == 0:
                            metadata = {
                                "git_branch": msg.get("gitBranch", "unknown"),
                                "slug": msg.get("slug", ""),
                                "cwd": msg.get("cwd", ""),
                                "version": msg.get("version", ""),
                            }

                        # Only include user/assistant messages
                        if msg.get("type") in ("user", "assistant"):
                            messages.append(msg)

                    except json.JSONDecodeError:
                        continue

        except Exception as e:
            return {"error": str(e)}

        # Get timestamps
        timestamps = [
            msg.get("timestamp") for msg in messages
            if msg.get("timestamp")
        ]

        # Count messages
        user_msgs = sum(1 for m in messages if m.get("type") == "user")
        assistant_msgs = sum(1 for m in messages if m.get("type") == "assistant")

        return {
            **metadata,
            "started_at": min(timestamps) if timestamps else None,
            "ended_at": max(timestamps) if timestamps else None,
            "user_messages": user_msgs,
            "assistant_messages": assistant_msgs,
            "messages": messages[-max_messages:],  # Last N messages for context
        }


class SyncClient:
    """Client for syncing sessions to Cognitex server."""

    # Batch size to avoid 413 Request Entity Too Large errors
    BATCH_SIZE = 5

    def __init__(self, config: SyncConfig):
        self.config = config
        self.discovery = SessionDiscovery(config)

    def check_connection(self) -> dict:
        """Check connection to Cognitex server."""
        if not self.config.server_url:
            return {"status": "error", "message": "Server URL not configured"}

        if not self.config.api_key:
            return {"status": "error", "message": "API key not configured"}

        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.get(
                    f"{self.config.server_url}/api/sync/status",
                    headers={"Authorization": f"Bearer {self.config.api_key}"},
                )

                if response.status_code == 200:
                    data = response.json()
                    return {"status": "ok", **data}
                elif response.status_code == 401:
                    return {"status": "error", "message": "Invalid API key"}
                elif response.status_code == 403:
                    return {"status": "error", "message": "Access denied"}
                else:
                    return {
                        "status": "error",
                        "message": f"Server error: {response.status_code}",
                    }

        except httpx.ConnectError:
            return {"status": "error", "message": f"Cannot connect to {self.config.server_url}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def sync_sessions(
        self,
        cli_type: str = "claude",
        force: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """Sync sessions to Cognitex server.

        Args:
            cli_type: Which CLI to sync
            force: Force re-sync of all sessions
            dry_run: Don't actually sync, just show what would be synced

        Returns:
            Sync results dict
        """
        # Load state to track what's been synced
        state = load_state()
        synced = state.get("synced_sessions", {})

        # Discover sessions
        all_sessions = self.discovery.discover_sessions(cli_type)

        # Filter to sessions that need syncing
        sessions_to_sync = []
        for session in all_sessions:
            session_key = f"{cli_type}:{session['session_id']}"
            last_synced = synced.get(session_key, {}).get("modified_ts", 0)

            # Sync if modified since last sync, or if forced
            if force or session["modified_ts"] > last_synced:
                sessions_to_sync.append(session)

        if not sessions_to_sync:
            return {
                "status": "ok",
                "message": "No new sessions to sync",
                "discovered": len(all_sessions),
                "synced": 0,
            }

        if dry_run:
            return {
                "status": "ok",
                "message": "Dry run - no changes made",
                "discovered": len(all_sessions),
                "would_sync": len(sessions_to_sync),
                "sessions": [s["session_id"] for s in sessions_to_sync],
            }

        # Parse and prepare session data
        sessions_data = []
        for session in sessions_to_sync:
            parsed = self.discovery.parse_session_file(session["session_file"])
            if parsed and not parsed.get("error"):
                sessions_data.append({
                    "session_id": session["session_id"],
                    "project_path": parsed.get("cwd") or session["project_path"],
                    "git_branch": parsed.get("git_branch", "unknown"),
                    "slug": parsed.get("slug", session["session_id"][:8]),
                    "started_at": parsed.get("started_at"),
                    "ended_at": parsed.get("ended_at"),
                    "user_messages": parsed.get("user_messages", 0),
                    "assistant_messages": parsed.get("assistant_messages", 0),
                    "messages": parsed.get("messages", []),
                })

        if not sessions_data:
            return {
                "status": "ok",
                "message": "No valid sessions to sync",
                "discovered": len(all_sessions),
                "synced": 0,
            }

        # Build lookup from session_id to original session metadata
        session_lookup = {s["session_id"]: s for s in sessions_to_sync}

        # Send to server in batches to avoid 413 errors
        total_synced = 0
        all_errors = []

        try:
            with httpx.Client(timeout=60.0) as client:
                for i in range(0, len(sessions_data), self.BATCH_SIZE):
                    batch = sessions_data[i:i + self.BATCH_SIZE]
                    batch_num = (i // self.BATCH_SIZE) + 1
                    total_batches = (len(sessions_data) + self.BATCH_SIZE - 1) // self.BATCH_SIZE

                    response = client.post(
                        f"{self.config.server_url}/api/sync/sessions",
                        headers={"Authorization": f"Bearer {self.config.api_key}"},
                        json={
                            "machine_id": self.config.machine_id,
                            "cli_type": cli_type,
                            "sessions": batch,
                        },
                    )

                    if response.status_code == 200:
                        result = response.json()
                        batch_synced = result.get("ingested", 0)
                        total_synced += batch_synced

                        if result.get("errors"):
                            all_errors.extend(result["errors"])

                        # Update state for successfully synced sessions in this batch
                        if batch_synced > 0:
                            for session_data in batch:
                                session_id = session_data["session_id"]
                                orig = session_lookup.get(session_id)
                                if orig:
                                    session_key = f"{cli_type}:{session_id}"
                                    synced[session_key] = {
                                        "modified_ts": orig["modified_ts"],
                                        "synced_at": datetime.now().isoformat(),
                                    }
                            state["synced_sessions"] = synced
                            state["last_sync"] = datetime.now().isoformat()
                            save_state(state)
                    else:
                        all_errors.append({
                            "batch": batch_num,
                            "error": f"Server error: {response.status_code}",
                            "detail": response.text[:200],
                        })

                return {
                    "status": "ok",
                    "discovered": len(all_sessions),
                    "attempted": len(sessions_data),
                    "synced": total_synced,
                    "batches": total_batches,
                    "errors": all_errors if all_errors else None,
                }

        except httpx.ConnectError:
            return {
                "status": "error",
                "message": f"Cannot connect to {self.config.server_url}",
                "synced_before_error": total_synced,
            }
        except Exception as e:
            return {
                "status": "error",
                "message": str(e),
                "synced_before_error": total_synced,
            }

    def sync_single_session(self, session_file: str, cli_type: str = "claude") -> dict:
        """Sync a single session file (for hook usage).

        Args:
            session_file: Path to the session file
            cli_type: CLI type

        Returns:
            Sync result dict
        """
        path = Path(session_file)
        if not path.exists():
            return {"status": "error", "message": f"Session file not found: {session_file}"}

        # Parse session
        parsed = self.discovery.parse_session_file(session_file)
        if parsed.get("error"):
            return {"status": "error", "message": parsed["error"]}

        # Extract project path from file location
        # e.g., ~/.claude/projects/-home-chris-projects-myapp/session.jsonl
        project_dir = path.parent.name
        project_path = "/" + project_dir.replace("-", "/")

        session_data = {
            "session_id": path.stem,
            "project_path": parsed.get("cwd") or project_path,
            "git_branch": parsed.get("git_branch", "unknown"),
            "slug": parsed.get("slug", path.stem[:8]),
            "started_at": parsed.get("started_at"),
            "ended_at": parsed.get("ended_at"),
            "user_messages": parsed.get("user_messages", 0),
            "assistant_messages": parsed.get("assistant_messages", 0),
            "messages": parsed.get("messages", []),
        }

        # Send to server
        try:
            with httpx.Client(timeout=60.0) as client:
                response = client.post(
                    f"{self.config.server_url}/api/sync/sessions",
                    headers={"Authorization": f"Bearer {self.config.api_key}"},
                    json={
                        "machine_id": self.config.machine_id,
                        "cli_type": cli_type,
                        "sessions": [session_data],
                    },
                )

                if response.status_code == 200:
                    return response.json()
                else:
                    return {
                        "status": "error",
                        "message": f"Server error: {response.status_code}",
                    }

        except Exception as e:
            return {"status": "error", "message": str(e)}
