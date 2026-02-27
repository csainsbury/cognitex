"""Bidirectional LEDGER.yaml <-> Neo4j sync for the Commitment Ledger (WP5).

LEDGER.yaml lives in ~/.cognitex/bootstrap/ alongside SOUL.md and USER.md,
giving the operator direct visibility and editability of their commitments.

- sync_graph_to_file: writes non-abandoned commitments from Neo4j to YAML
- sync_file_to_graph: reads YAML, pushes status/field changes back to Neo4j
"""

import asyncio
from datetime import datetime
from pathlib import Path

import structlog
import yaml

from cognitex.agent.bootstrap import BOOTSTRAP_DIR

logger = structlog.get_logger()


class LedgerSyncService:
    """Bidirectional sync between Commitment nodes in Neo4j and LEDGER.yaml."""

    def __init__(self, ledger_path: Path | None = None):
        self.ledger_path = ledger_path or (BOOTSTRAP_DIR / "LEDGER.yaml")
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Ensure LEDGER.yaml exists with empty template."""
        if not self.ledger_path.exists():
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            template = {
                "commitments": [],
                "last_synced": datetime.now().isoformat(),
            }
            await self._write_yaml(template)
            logger.info("Created LEDGER.yaml", path=str(self.ledger_path))

    async def sync_graph_to_file(self) -> None:
        """Query non-abandoned commitments from Neo4j, write LEDGER.yaml."""
        from cognitex.db.graph_schema import get_commitments
        from cognitex.db.neo4j import get_neo4j_session

        try:
            async for session in get_neo4j_session():
                all_commitments = await get_commitments(session, limit=200)
                break

            # Filter out abandoned
            active = [c for c in all_commitments if c.get("status") != "abandoned"]

            entries = []
            for c in active:
                deadline = c.get("deadline")
                if deadline and hasattr(deadline, "isoformat"):
                    deadline = deadline.isoformat()[:10]
                elif deadline:
                    deadline = str(deadline)[:10]

                date_logged = c.get("date_logged")
                if date_logged and hasattr(date_logged, "isoformat"):
                    date_logged = date_logged.isoformat()[:10]
                elif date_logged:
                    date_logged = str(date_logged)[:10]

                entries.append(
                    {
                        "id": c.get("id", ""),
                        "action": c.get("task_description", ""),
                        "owner": c.get("owner", ""),
                        "deadline": deadline,
                        "status": c.get("status", "pending"),
                        "cognitive_load": c.get("cognitive_load", "medium"),
                        "source": c.get("source", ""),
                        "project": c.get("project_title") or None,
                        "waiting_on": c.get("waiting_on_email") or None,
                        "date_logged": date_logged,
                    }
                )

            data = {
                "commitments": entries,
                "last_synced": datetime.now().isoformat(),
            }
            await self._write_yaml(data)
            logger.debug("Synced commitments to LEDGER.yaml", count=len(entries))

        except Exception as e:
            logger.warning("Failed to sync graph to LEDGER.yaml", error=str(e))

    async def sync_file_to_graph(self) -> int:
        """Read LEDGER.yaml, update graph nodes where fields differ.

        Returns:
            Number of commitments updated in the graph.
        """
        from cognitex.db.graph_schema import get_commitments, update_commitment
        from cognitex.db.neo4j import get_neo4j_session

        change_count = 0

        try:
            data = await self._read_yaml()
            if data is None:
                return 0

            file_commitments = data.get("commitments", [])
            if not file_commitments:
                return 0

            # Build lookup of file commitments by id
            file_lookup = {c["id"]: c for c in file_commitments if c.get("id")}

            async for session in get_neo4j_session():
                graph_commitments = await get_commitments(session, limit=200)
                break

            graph_lookup = {c.get("id"): c for c in graph_commitments if c.get("id")}

            # Compare and update
            async for session in get_neo4j_session():
                for cid, file_c in file_lookup.items():
                    graph_c = graph_lookup.get(cid)
                    if not graph_c:
                        continue

                    # Check for changes in editable fields
                    updates: dict[str, str | None] = {}

                    file_status = file_c.get("status", "")
                    graph_status = graph_c.get("status", "")
                    if file_status and file_status != graph_status:
                        updates["status"] = file_status

                    file_desc = file_c.get("action", "")
                    graph_desc = graph_c.get("task_description", "")
                    if file_desc and file_desc != graph_desc:
                        updates["task_description"] = file_desc

                    file_load = file_c.get("cognitive_load", "")
                    graph_load = graph_c.get("cognitive_load", "")
                    if file_load and file_load != graph_load:
                        updates["cognitive_load"] = file_load

                    file_deadline = file_c.get("deadline")
                    if file_deadline and isinstance(file_deadline, str):
                        graph_deadline = graph_c.get("deadline")
                        if graph_deadline and hasattr(graph_deadline, "isoformat"):
                            graph_deadline_str = graph_deadline.isoformat()[:10]
                        else:
                            graph_deadline_str = str(graph_deadline)[:10] if graph_deadline else ""
                        if file_deadline != graph_deadline_str:
                            updates["deadline"] = file_deadline

                    if updates:
                        await update_commitment(session, cid, **updates)
                        change_count += 1
                        logger.debug(
                            "Updated commitment from LEDGER.yaml",
                            commitment_id=cid,
                            changes=list(updates.keys()),
                        )
                break

            if change_count:
                logger.info(
                    "Synced LEDGER.yaml changes to graph",
                    changes=change_count,
                )

        except Exception as e:
            logger.warning("Failed to sync LEDGER.yaml to graph", error=str(e))

        return change_count

    async def _write_yaml(self, data: dict) -> None:
        """Write data to LEDGER.yaml with lock."""
        async with self._write_lock:
            content = (
                "# Cognitex Commitment Ledger\n"
                "# Edit status fields to update commitments. "
                "Changes sync on next boot.\n"
            )
            content += yaml.dump(
                data,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            )
            await asyncio.to_thread(self.ledger_path.write_text, content)

    async def _read_yaml(self) -> dict | None:
        """Read and parse LEDGER.yaml."""
        if not self.ledger_path.exists():
            return None

        try:
            raw = await asyncio.to_thread(self.ledger_path.read_text)
            data = yaml.safe_load(raw)
            if not isinstance(data, dict):
                logger.warning("LEDGER.yaml is not a valid mapping")
                return None
            return data
        except yaml.YAMLError as e:
            logger.warning("Malformed LEDGER.yaml", error=str(e))
            return None
        except Exception as e:
            logger.warning("Failed to read LEDGER.yaml", error=str(e))
            return None


# Singleton
_service: LedgerSyncService | None = None


def get_ledger_sync_service() -> LedgerSyncService:
    """Get or create the ledger sync service singleton."""
    global _service
    if _service is None:
        _service = LedgerSyncService()
    return _service
