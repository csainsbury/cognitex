"""Tests for WP5: Commitment Ledger.

Tests cover skill loading, schema CRUD, extraction pipeline,
monitoring queries, LEDGER.yaml sync, and inbox integration.
"""

import json
import shutil
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cognitex.agent.skills import SkillsLoader

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_skill_dirs(tmp_path):
    """Create temp bundled + user skill directories."""
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    bundled.mkdir()
    user.mkdir()
    return bundled, user


@pytest.fixture
def skill_loader(tmp_skill_dirs):
    """SkillsLoader pointed at temp directories."""
    bundled, user = tmp_skill_dirs
    return SkillsLoader(bundled_dir=bundled, user_dir=user)


@pytest.fixture
def install_commitment_skill(tmp_skill_dirs):
    """Install the real commitment-extraction SKILL.md into the temp bundled dir."""
    bundled, _ = tmp_skill_dirs
    src = Path(__file__).parent.parent / "src" / "cognitex" / "skills" / "commitment-extraction"
    dest = bundled / "commitment-extraction"
    shutil.copytree(src, dest)


@pytest.fixture
def ledger_path(tmp_path):
    """Provide a temp path for LEDGER.yaml."""
    return tmp_path / "LEDGER.yaml"


def _make_llm_mock(response_data: dict) -> AsyncMock:
    """Create a mock LLM service that returns the given dict as JSON."""
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(response_data))
    return llm


# Mock Neo4j session that records calls
class MockNeo4jSession:
    def __init__(self):
        self.queries = []

    async def run(self, query, **params):
        self.queries.append({"query": query, "params": params})
        # Return a dict-like record for MERGE/MATCH ... RETURN c queries
        mock_node = {
            "id": params.get("commitment_id", "commit_test123"),
            "task_description": params.get("task_description", "Test"),
            "owner": params.get("owner", "test@example.com"),
            "status": params.get("status", "pending"),
            "deadline": None,
            "cognitive_load": params.get("cognitive_load", "medium"),
            "source": params.get("source"),
            "source_id": params.get("source_id"),
            "dependencies": [],
            "date_logged": "2026-02-27",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
        result = AsyncMock()
        single_record = MagicMock()
        single_record.__getitem__ = lambda _self, _key: mock_node
        result.single = AsyncMock(return_value=single_record)
        result.data = AsyncMock(return_value=[{"c": mock_node}])
        return result


# ---------------------------------------------------------------------------
# Test: Skill loading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("install_commitment_skill")
async def test_commitment_skill_loads(skill_loader):
    """Skill loads and parses frontmatter correctly."""
    skill = await skill_loader.get_skill("commitment-extraction")
    assert skill is not None
    assert skill.name == "commitment-extraction"
    assert skill.format == "agentskills"
    assert "commitment" in skill.purpose.lower() or "commitment" in skill.raw_content.lower()


@pytest.mark.asyncio
@pytest.mark.usefixtures("install_commitment_skill")
async def test_commitment_skill_has_output_schema(skill_loader):
    """Skill raw content contains the expected output schema fields."""
    skill = await skill_loader.get_skill("commitment-extraction")
    assert skill is not None
    assert '"action"' in skill.raw_content
    assert '"deadline"' in skill.raw_content
    assert '"cognitive_load"' in skill.raw_content
    assert '"confidence"' in skill.raw_content


# ---------------------------------------------------------------------------
# Test: graph_schema CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_commitment():
    """Node created with correct properties."""
    from cognitex.db.graph_schema import create_commitment

    session = MockNeo4jSession()
    result = await create_commitment(
        session,
        commitment_id="commit_abc123",
        task_description="Send quarterly report",
        owner="sarah@example.com",
        status="pending",
        deadline="2026-03-01",
        cognitive_load="medium",
        source="email",
        source_id="gmail_xyz",
    )
    assert result["id"] == "commit_abc123"
    assert len(session.queries) == 1
    assert "MERGE (c:Commitment" in session.queries[0]["query"]


@pytest.mark.asyncio
async def test_update_commitment_status():
    """Status transitions work."""
    from cognitex.db.graph_schema import update_commitment

    session = MockNeo4jSession()
    result = await update_commitment(
        session,
        commitment_id="commit_abc123",
        status="accepted",
    )
    assert result is not None
    assert len(session.queries) == 1
    assert "c.status = $status" in session.queries[0]["query"]
    assert session.queries[0]["params"]["status"] == "accepted"


@pytest.mark.asyncio
async def test_link_commitment_to_email():
    """EXTRACTED_FROM relationship created."""
    from cognitex.db.graph_schema import link_commitment_to_email

    session = MockNeo4jSession()
    await link_commitment_to_email(session, "commit_abc123", "gmail_xyz")
    assert len(session.queries) == 1
    assert "EXTRACTED_FROM" in session.queries[0]["query"]


@pytest.mark.asyncio
async def test_link_commitment_to_person():
    """OWNED_BY and WAITING_ON relationships."""
    from cognitex.db.graph_schema import link_commitment_to_person

    session = MockNeo4jSession()

    await link_commitment_to_person(session, "commit_abc123", "user@example.com", "OWNED_BY")
    assert "OWNED_BY" in session.queries[0]["query"]

    await link_commitment_to_person(session, "commit_abc123", "other@example.com", "WAITING_ON")
    assert "WAITING_ON" in session.queries[1]["query"]


@pytest.mark.asyncio
async def test_link_commitment_to_person_rejects_invalid_rel():
    """Invalid rel_type falls back to OWNED_BY."""
    from cognitex.db.graph_schema import link_commitment_to_person

    session = MockNeo4jSession()
    await link_commitment_to_person(session, "commit_abc123", "user@example.com", "INVALID")
    assert "OWNED_BY" in session.queries[0]["query"]


@pytest.mark.asyncio
async def test_get_commitments():
    """Query with optional filters returns results."""
    from cognitex.db.graph_schema import get_commitments

    session = MockNeo4jSession()
    await get_commitments(session, status="pending", limit=10)
    assert len(session.queries) == 1
    assert "c.status = $status" in session.queries[0]["query"]


# ---------------------------------------------------------------------------
# Test: Extraction pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extraction_uses_skill():
    """Skill is loaded and injected into prompt when available."""
    from cognitex.agent.graph_observer import GraphObserver

    observer = GraphObserver()

    mock_skill = MagicMock()
    mock_skill.raw_content = "# Commitment Extraction Skill"

    mock_loader = AsyncMock()
    mock_loader.get_skill = AsyncMock(return_value=mock_skill)

    llm_response = json.dumps({"commitments": []})
    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=llm_response)

    sent_emails = [
        {
            "gmail_id": "gmail_001",
            "subject": "Re: Report",
            "body": "I'll send you the report by Friday. Let me know if you need anything else.",
            "date": "2026-02-27",
            "recipient": "sarah@example.com",
        }
    ]

    with (
        patch("cognitex.db.neo4j.run_query", new_callable=AsyncMock) as mock_run_query,
        patch("cognitex.services.llm.get_llm_service", return_value=mock_llm),
        patch("cognitex.agent.skills.get_skills_loader", return_value=mock_loader),
    ):
        # First call returns sent emails, second call returns [] (no existing)
        mock_run_query.side_effect = [sent_emails, []]
        await observer.extract_commitments_from_emails()

    # Verify skill was loaded
    mock_loader.get_skill.assert_called_once_with("commitment-extraction")

    # Verify LLM was called with skill content in prompt
    call_args = mock_llm.complete.call_args
    assert "Commitment Extraction Skill" in call_args[0][0]


@pytest.mark.asyncio
async def test_extraction_creates_nodes_and_inbox():
    """Mock LLM response creates Commitment nodes and inbox items."""
    from cognitex.agent.graph_observer import GraphObserver

    observer = GraphObserver()

    llm_response = json.dumps(
        {
            "commitments": [
                {
                    "action": "Send quarterly report",
                    "deadline": "2026-03-01",
                    "deadline_source": "explicit",
                    "recipient": "sarah@example.com",
                    "cognitive_load": "medium",
                    "confidence": 0.95,
                }
            ]
        }
    )

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=llm_response)

    mock_loader = AsyncMock()
    mock_loader.get_skill = AsyncMock(return_value=None)

    sent_emails = [
        {
            "gmail_id": "gmail_001",
            "subject": "Re: Report",
            "body": "I'll send you the report by Friday. Let me know if you need anything else.",
            "date": "2026-02-27",
            "recipient": "sarah@example.com",
        }
    ]

    mock_session = MockNeo4jSession()
    mock_inbox = AsyncMock()

    async def mock_get_session():
        yield mock_session

    with (
        patch("cognitex.db.neo4j.run_query", new_callable=AsyncMock) as mock_run_query,
        patch("cognitex.services.llm.get_llm_service", return_value=mock_llm),
        patch("cognitex.agent.skills.get_skills_loader", return_value=mock_loader),
        patch("cognitex.db.graph_schema.create_commitment", new_callable=AsyncMock) as mock_create,
        patch("cognitex.db.graph_schema.link_commitment_to_email", new_callable=AsyncMock),
        patch("cognitex.db.graph_schema.link_commitment_to_person", new_callable=AsyncMock),
        patch("cognitex.db.neo4j.get_neo4j_session", mock_get_session),
        patch("cognitex.services.inbox.get_inbox_service", return_value=mock_inbox),
    ):
        mock_run_query.side_effect = [sent_emails, []]
        mock_create.return_value = {"id": "commit_test"}

        commitments = await observer.extract_commitments_from_emails()

    assert len(commitments) == 1
    assert commitments[0]["action"] == "Send quarterly report"
    mock_inbox.create_item.assert_called_once()
    call_kwargs = mock_inbox.create_item.call_args[1]
    assert call_kwargs["item_type"] == "commitment_proposal"


@pytest.mark.asyncio
async def test_extraction_deduplication():
    """Same email twice produces no duplicate nodes."""
    from cognitex.agent.graph_observer import GraphObserver

    observer = GraphObserver()

    mock_llm = AsyncMock()
    mock_loader = AsyncMock()
    mock_loader.get_skill = AsyncMock(return_value=None)

    sent_emails = [
        {
            "gmail_id": "gmail_001",
            "subject": "Re: Report",
            "body": "I'll send you the report by Friday.",
            "date": "2026-02-27",
            "recipient": "sarah@example.com",
        }
    ]

    with (
        patch("cognitex.db.neo4j.run_query", new_callable=AsyncMock) as mock_run_query,
        patch("cognitex.services.llm.get_llm_service", return_value=mock_llm),
        patch("cognitex.agent.skills.get_skills_loader", return_value=mock_loader),
    ):
        # First call returns sent emails, second returns existing commitment
        mock_run_query.side_effect = [sent_emails, [{"id": "commit_existing"}]]
        commitments = await observer.extract_commitments_from_emails()

    # LLM should NOT have been called since dedup found existing
    mock_llm.complete.assert_not_called()
    assert len(commitments) == 0


@pytest.mark.asyncio
async def test_extraction_heuristic_filter():
    """Emails without commitment keywords are skipped."""
    from cognitex.agent.graph_observer import GraphObserver

    observer = GraphObserver()

    mock_llm = AsyncMock()
    mock_loader = AsyncMock()
    mock_loader.get_skill = AsyncMock(return_value=None)

    sent_emails = [
        {
            "gmail_id": "gmail_001",
            "subject": "FYI",
            "body": "Here is the report you requested. The data is from Q4. No further action needed from my side.",
            "date": "2026-02-27",
            "recipient": "sarah@example.com",
        }
    ]

    with (
        patch("cognitex.db.neo4j.run_query", new_callable=AsyncMock) as mock_run_query,
        patch("cognitex.services.llm.get_llm_service", return_value=mock_llm),
        patch("cognitex.agent.skills.get_skills_loader", return_value=mock_loader),
    ):
        mock_run_query.return_value = sent_emails
        commitments = await observer.extract_commitments_from_emails()

    # LLM should not be called for emails without commitment keywords
    mock_llm.complete.assert_not_called()
    assert len(commitments) == 0


# ---------------------------------------------------------------------------
# Test: Monitoring queries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approaching_commitments_query():
    """Deadlines within 48h are returned."""
    from cognitex.agent.graph_observer import GraphObserver

    observer = GraphObserver()

    mock_data = [
        {
            "id": "commit_001",
            "description": "Send report",
            "deadline": "2026-02-28",
            "status": "accepted",
            "owner": "user@example.com",
            "cognitive_load": "medium",
            "project": None,
            "waiting_on": None,
        }
    ]

    with patch("cognitex.db.neo4j.run_query", new_callable=AsyncMock) as mock_rq:
        mock_rq.return_value = mock_data
        result = await observer.get_approaching_commitments(hours=48)

    assert len(result) == 1
    assert result[0]["id"] == "commit_001"
    # Verify the query uses correct status filter
    call_query = mock_rq.call_args[0][0]
    assert "accepted" in call_query
    assert "in_progress" in call_query


@pytest.mark.asyncio
async def test_overdue_commitments_query():
    """Past-deadline commitments returned."""
    from cognitex.agent.graph_observer import GraphObserver

    observer = GraphObserver()

    mock_data = [
        {
            "id": "commit_002",
            "description": "Review PR",
            "deadline": "2026-02-25",
            "status": "in_progress",
            "owner": "user@example.com",
            "cognitive_load": "low",
            "project": None,
            "waiting_on": None,
        }
    ]

    with patch("cognitex.db.neo4j.run_query", new_callable=AsyncMock) as mock_rq:
        mock_rq.return_value = mock_data
        result = await observer.get_overdue_commitments()

    assert len(result) == 1
    assert result[0]["id"] == "commit_002"
    call_query = mock_rq.call_args[0][0]
    assert "c.deadline < datetime()" in call_query


@pytest.mark.asyncio
async def test_stale_commitments_query():
    """Blocked >7d commitments returned."""
    from cognitex.agent.graph_observer import GraphObserver

    observer = GraphObserver()

    mock_data = [
        {
            "id": "commit_003",
            "description": "Waiting for legal review",
            "deadline": None,
            "status": "blocked",
            "owner": "user@example.com",
            "cognitive_load": "high",
            "last_updated": "2026-02-15",
            "project": None,
            "waiting_on": "legal@example.com",
        }
    ]

    with patch("cognitex.db.neo4j.run_query", new_callable=AsyncMock) as mock_rq:
        mock_rq.return_value = mock_data
        result = await observer.get_stale_commitments(days=7)

    assert len(result) == 1
    assert result[0]["status"] == "blocked"
    call_query = mock_rq.call_args[0][0]
    assert "blocked" in call_query
    assert "waiting_on" in call_query


# ---------------------------------------------------------------------------
# Test: get_full_context includes commitments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_context_includes_commitments():
    """`get_full_context()` returns commitment data in context dict."""
    from cognitex.agent.graph_observer import GraphObserver

    observer = GraphObserver()

    # Mock all the gather calls to return empty or defaults
    async def mock_empty_list():
        return []

    async def mock_empty_set():
        return set()

    with (
        patch.object(observer, "_get_inbox_items", side_effect=mock_empty_list),
        patch.object(observer, "get_recent_changes", side_effect=mock_empty_list),
        patch.object(observer, "get_stale_items", side_effect=mock_empty_list),
        patch.object(observer, "get_orphaned_nodes", side_effect=mock_empty_list),
        patch.object(observer, "get_goal_health", side_effect=mock_empty_list),
        patch.object(observer, "get_project_health", side_effect=mock_empty_list),
        patch.object(observer, "get_pending_tasks", side_effect=mock_empty_list),
        patch.object(observer, "get_recent_documents", side_effect=mock_empty_list),
        patch.object(observer, "get_connection_opportunities", side_effect=mock_empty_list),
        patch.object(observer, "get_user_writing_samples", side_effect=mock_empty_list),
        patch.object(observer, "get_actionable_emails", side_effect=mock_empty_list),
        patch.object(observer, "get_pending_calendar_blocks", side_effect=mock_empty_list),
        patch.object(observer, "get_projects_with_recent_blocks", side_effect=mock_empty_set),
        patch.object(
            observer,
            "get_approaching_commitments",
            return_value=[{"id": "c1", "description": "Test"}],
        ),
        patch.object(
            observer,
            "get_overdue_commitments",
            return_value=[{"id": "c2", "description": "Overdue"}],
        ),
        patch.object(observer, "get_stale_commitments", return_value=[]),
    ):
        context = await observer.get_full_context()

    assert "approaching_commitments" in context
    assert "overdue_commitments" in context
    assert "stale_commitments" in context
    assert len(context["approaching_commitments"]) == 1
    assert len(context["overdue_commitments"]) == 1
    assert context["summary"]["approaching_commitment_count"] == 1
    assert context["summary"]["overdue_commitment_count"] == 1
    assert context["summary"]["stale_commitment_count"] == 0


# ---------------------------------------------------------------------------
# Test: LEDGER.yaml sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_graph_to_file(ledger_path):
    """Commitments written to YAML correctly."""
    from cognitex.services.ledger_sync import LedgerSyncService

    svc = LedgerSyncService(ledger_path=ledger_path)

    mock_commitments = [
        {
            "id": "commit_abc123",
            "task_description": "Send quarterly report",
            "owner": "sarah@example.com",
            "deadline": None,
            "status": "accepted",
            "cognitive_load": "medium",
            "source": "email",
            "source_id": "gmail_xyz",
            "date_logged": "2026-02-27",
            "project_title": "Q1 Reporting",
            "waiting_on_email": None,
        }
    ]

    mock_session = AsyncMock()

    async def mock_get_session():
        yield mock_session

    with (
        patch("cognitex.db.neo4j.get_neo4j_session", mock_get_session),
        patch("cognitex.db.graph_schema.get_commitments", new_callable=AsyncMock) as mock_gc,
    ):
        mock_gc.return_value = mock_commitments
        await svc.sync_graph_to_file()

    assert ledger_path.exists()
    import yaml

    data = yaml.safe_load(ledger_path.read_text())
    assert len(data["commitments"]) == 1
    assert data["commitments"][0]["id"] == "commit_abc123"
    assert data["commitments"][0]["action"] == "Send quarterly report"
    assert data["last_synced"] is not None


@pytest.mark.asyncio
async def test_sync_file_to_graph(ledger_path):
    """YAML changes propagated to graph."""
    import yaml

    from cognitex.services.ledger_sync import LedgerSyncService

    svc = LedgerSyncService(ledger_path=ledger_path)

    # Write a LEDGER.yaml with a status change
    ledger_data = {
        "commitments": [
            {
                "id": "commit_abc123",
                "action": "Send quarterly report",
                "owner": "sarah@example.com",
                "deadline": "2026-03-01",
                "status": "complete",  # Changed from accepted
                "cognitive_load": "medium",
                "source": "email",
                "project": None,
                "waiting_on": None,
                "date_logged": "2026-02-27",
            }
        ],
        "last_synced": "2026-02-27T10:00:00",
    }
    ledger_path.write_text(yaml.dump(ledger_data))

    # Graph has the commitment in "accepted" state
    graph_commitments = [
        {
            "id": "commit_abc123",
            "task_description": "Send quarterly report",
            "owner": "sarah@example.com",
            "deadline": "2026-03-01",
            "status": "accepted",
            "cognitive_load": "medium",
        }
    ]

    mock_session = AsyncMock()

    async def mock_get_session():
        yield mock_session

    with (
        patch("cognitex.db.neo4j.get_neo4j_session", mock_get_session),
        patch("cognitex.db.graph_schema.get_commitments", new_callable=AsyncMock) as mock_gc,
        patch("cognitex.db.graph_schema.update_commitment", new_callable=AsyncMock) as mock_uc,
    ):
        mock_gc.return_value = graph_commitments
        mock_uc.return_value = {}
        changes = await svc.sync_file_to_graph()

    assert changes == 1
    mock_uc.assert_called_once()
    call_kwargs = mock_uc.call_args[1]
    assert call_kwargs.get("status") == "complete"


@pytest.mark.asyncio
async def test_sync_handles_missing_file(ledger_path):
    """Graceful when LEDGER.yaml doesn't exist."""
    from cognitex.services.ledger_sync import LedgerSyncService

    svc = LedgerSyncService(ledger_path=ledger_path)
    # File doesn't exist — should return 0, not crash
    changes = await svc.sync_file_to_graph()
    assert changes == 0


@pytest.mark.asyncio
async def test_sync_handles_malformed_yaml(ledger_path):
    """Graceful on invalid YAML."""
    from cognitex.services.ledger_sync import LedgerSyncService

    svc = LedgerSyncService(ledger_path=ledger_path)
    ledger_path.write_text(": [invalid yaml\n  broken: {")
    changes = await svc.sync_file_to_graph()
    assert changes == 0
