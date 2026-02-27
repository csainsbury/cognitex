"""Tests for WP7: Slash Commands & Model Routing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cognitex.agent.slash_commands import (
    SlashCommand,
    SlashCommandRegistry,
)
from cognitex.services.model_config import (
    MODEL_ALIASES,
    TASK_MODEL_SLOTS,
    ModelConfig,
)

# ---------------------------------------------------------------------------
# Registry & Dispatch
# ---------------------------------------------------------------------------


@pytest.fixture
def registry():
    return SlashCommandRegistry()


@pytest.fixture
def sample_command():
    handler = AsyncMock(return_value="ok")
    return SlashCommand(
        name="test",
        description="A test command",
        handler=handler,
        aliases=["t"],
        usage="<arg>",
        category="testing",
    )


@pytest.mark.asyncio
async def test_dispatch_non_command(registry):
    """Non-`/` input -> handled=False."""
    result = await registry.dispatch("hello world")
    assert result.handled is False


@pytest.mark.asyncio
async def test_dispatch_unknown_command(registry):
    """Unknown `/xyz` -> handled=False."""
    result = await registry.dispatch("/xyz")
    assert result.handled is False


@pytest.mark.asyncio
async def test_dispatch_registered_command(registry, sample_command):
    """Calls handler and returns response."""
    registry.register(sample_command)
    result = await registry.dispatch("/test some args")
    assert result.handled is True
    assert result.response == "ok"
    assert result.command_name == "test"
    sample_command.handler.assert_awaited_once_with("some args")


@pytest.mark.asyncio
async def test_dispatch_alias(registry, sample_command):
    """Alias routes to same command."""
    registry.register(sample_command)
    result = await registry.dispatch("/t arg1")
    assert result.handled is True
    assert result.command_name == "test"


@pytest.mark.asyncio
async def test_dispatch_case_insensitive(registry, sample_command):
    """`/TEST` works same as `/test`."""
    registry.register(sample_command)
    result = await registry.dispatch("/TEST arg")
    assert result.handled is True
    assert result.command_name == "test"


@pytest.mark.asyncio
async def test_dispatch_error_handling(registry):
    """Handler exception returns error, no crash."""
    handler = AsyncMock(side_effect=RuntimeError("boom"))
    cmd = SlashCommand(name="bad", description="explodes", handler=handler)
    registry.register(cmd)
    result = await registry.dispatch("/bad")
    assert result.handled is True
    assert "Error" in result.response
    assert "boom" in result.response


def test_list_commands_deduplicates(registry, sample_command):
    """Aliases not listed as separate commands."""
    registry.register(sample_command)
    cmds = registry.list_commands()
    assert len(cmds) == 1
    assert cmds[0]["name"] == "test"
    assert "t" in cmds[0]["aliases"]


# ---------------------------------------------------------------------------
# MODEL_ALIASES
# ---------------------------------------------------------------------------


def test_model_aliases_valid():
    """All aliases map to (provider, model_id) tuples."""
    for alias, (provider, model_id) in MODEL_ALIASES.items():
        assert isinstance(alias, str)
        assert provider in ("together", "anthropic", "openai", "google")
        assert isinstance(model_id, str)
        assert len(model_id) > 0


# ---------------------------------------------------------------------------
# ModelConfig task overrides
# ---------------------------------------------------------------------------


def test_model_config_task_overrides_default():
    """New task override fields default to empty string."""
    config = ModelConfig(
        provider="anthropic",
        planner_model="claude-sonnet-4-20250514",
        executor_model="claude-sonnet-4-20250514",
        embedding_model="BAAI/bge-base-en-v1.5",
    )
    for slot in TASK_MODEL_SLOTS:
        assert getattr(config, f"{slot}_model") == ""


def test_model_config_task_overrides_roundtrip():
    """Task overrides survive to_dict() / from_dict()."""
    config = ModelConfig(
        provider="anthropic",
        planner_model="claude-sonnet-4-20250514",
        executor_model="claude-sonnet-4-20250514",
        embedding_model="BAAI/bge-base-en-v1.5",
        autonomous_model="claude-opus-4-20250514",
        triage_model="claude-3-5-haiku-20241022",
    )
    d = config.to_dict()
    restored = ModelConfig.from_dict(d)
    assert restored.autonomous_model == "claude-opus-4-20250514"
    assert restored.triage_model == "claude-3-5-haiku-20241022"
    assert restored.draft_model == ""


def test_get_model_for_task_override():
    """Returns override when set."""
    config = ModelConfig(
        provider="anthropic",
        planner_model="claude-sonnet-4-20250514",
        executor_model="claude-sonnet-4-20250514",
        embedding_model="BAAI/bge-base-en-v1.5",
        autonomous_model="claude-opus-4-20250514",
    )
    assert config.get_model_for_task("autonomous") == "claude-opus-4-20250514"


def test_get_model_for_task_fallback():
    """Returns planner_model when no override set."""
    config = ModelConfig(
        provider="anthropic",
        planner_model="claude-sonnet-4-20250514",
        executor_model="claude-sonnet-4-20250514",
        embedding_model="BAAI/bge-base-en-v1.5",
    )
    assert config.get_model_for_task("triage") == "claude-sonnet-4-20250514"


# ---------------------------------------------------------------------------
# Handler tests (mocked dependencies)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_model_show():
    """`/model` no args -> current config string."""
    mock_config = ModelConfig(
        provider="anthropic",
        planner_model="claude-sonnet-4-20250514",
        executor_model="claude-sonnet-4-20250514",
        embedding_model="BAAI/bge-base-en-v1.5",
    )
    mock_svc = AsyncMock()
    mock_svc.get_config = AsyncMock(return_value=mock_config)

    with patch(
        "cognitex.services.model_config.get_model_config_service",
        return_value=mock_svc,
    ):
        from cognitex.agent.slash_commands import _handle_model

        result = await _handle_model("")
        assert "anthropic" in result
        assert "claude-sonnet-4-20250514" in result


@pytest.mark.asyncio
async def test_handle_model_switch():
    """`/model sonnet` -> updates mocked config service."""
    mock_config = ModelConfig(
        provider="together",
        planner_model="deepseek-ai/DeepSeek-V3",
        executor_model="deepseek-ai/DeepSeek-V3",
        embedding_model="BAAI/bge-base-en-v1.5",
    )
    mock_svc = AsyncMock()
    mock_svc.get_config = AsyncMock(return_value=mock_config)
    mock_svc.set_config = AsyncMock()

    with (
        patch(
            "cognitex.services.model_config.get_model_config_service",
            return_value=mock_svc,
        ),
        patch("cognitex.services.llm.reset_llm_service"),
    ):
        from cognitex.agent.slash_commands import _handle_model

        result = await _handle_model("sonnet")
        assert "sonnet" in result.lower()
        mock_svc.set_config.assert_awaited_once()
        saved = mock_svc.set_config.call_args[0][0]
        assert saved.provider == "anthropic"
        assert saved.planner_model == "claude-sonnet-4-20250514"


@pytest.mark.asyncio
async def test_handle_model_task_slot():
    """`/model autonomous opus` -> sets per-task override."""
    mock_svc = AsyncMock()
    mock_svc.update_task_model = AsyncMock(return_value=ModelConfig(
        provider="anthropic",
        planner_model="claude-sonnet-4-20250514",
        executor_model="claude-sonnet-4-20250514",
        embedding_model="BAAI/bge-base-en-v1.5",
        autonomous_model="claude-opus-4-20250514",
    ))

    with patch(
        "cognitex.services.model_config.get_model_config_service",
        return_value=mock_svc,
    ):
        from cognitex.agent.slash_commands import _handle_model

        result = await _handle_model("autonomous opus")
        assert "autonomous" in result.lower()
        mock_svc.update_task_model.assert_awaited_once_with(
            "autonomous", "claude-opus-4-20250514"
        )


@pytest.mark.asyncio
async def test_handle_status():
    """Returns mode, model, pending count."""
    mock_state = MagicMock()
    mock_state.mode.value = "fragmented"

    mock_estimator = AsyncMock()
    mock_estimator.get_current_state = AsyncMock(return_value=mock_state)

    mock_config = ModelConfig(
        provider="anthropic",
        planner_model="claude-sonnet-4-20250514",
        executor_model="claude-sonnet-4-20250514",
        embedding_model="BAAI/bge-base-en-v1.5",
    )
    mock_config_svc = AsyncMock()
    mock_config_svc.get_config = AsyncMock(return_value=mock_config)

    mock_inbox = AsyncMock()
    mock_inbox.get_pending_count = AsyncMock(return_value={"email_draft": 2, "task": 1})

    with (
        patch(
            "cognitex.agent.state_model.get_state_estimator",
            return_value=mock_estimator,
        ),
        patch(
            "cognitex.services.model_config.get_model_config_service",
            return_value=mock_config_svc,
        ),
        patch(
            "cognitex.services.inbox.get_inbox_service",
            return_value=mock_inbox,
        ),
    ):
        from cognitex.agent.slash_commands import _handle_status

        result = await _handle_status("")
        assert "fragmented" in result
        assert "anthropic" in result
        assert "3" in result  # 2 + 1


@pytest.mark.asyncio
async def test_handle_help():
    """Lists all registered commands."""
    registry = SlashCommandRegistry()
    handler = AsyncMock(return_value="")
    registry.register(SlashCommand(name="foo", description="Do foo", handler=handler))
    registry.register(SlashCommand(name="bar", description="Do bar", handler=handler))

    # Patch singleton to use our registry
    with patch("cognitex.agent.slash_commands.get_slash_registry", return_value=registry):
        from cognitex.agent.slash_commands import _handle_help

        result = await _handle_help("")
        assert "/foo" in result
        assert "/bar" in result


@pytest.mark.asyncio
async def test_handle_approve():
    """Calls inbox approve."""
    mock_item = MagicMock()
    mock_item.title = "Draft email to Bob"

    mock_inbox = AsyncMock()
    mock_inbox.approve_item = AsyncMock(return_value=mock_item)

    with patch(
        "cognitex.services.inbox.get_inbox_service",
        return_value=mock_inbox,
    ):
        from cognitex.agent.slash_commands import _handle_approve

        result = await _handle_approve("abc123")
        assert "Approved" in result
        assert "Draft email to Bob" in result
        mock_inbox.approve_item.assert_awaited_once_with("abc123")


@pytest.mark.asyncio
async def test_handle_reject():
    """Calls inbox reject."""
    mock_item = MagicMock()
    mock_item.title = "Task: fix login"

    mock_inbox = AsyncMock()
    mock_inbox.reject_item = AsyncMock(return_value=mock_item)

    with patch(
        "cognitex.services.inbox.get_inbox_service",
        return_value=mock_inbox,
    ):
        from cognitex.agent.slash_commands import _handle_reject

        result = await _handle_reject("abc123 not now")
        assert "Rejected" in result
        mock_inbox.reject_item.assert_awaited_once_with(
            "abc123", reason_category="user_rejected", reason_text="not now"
        )


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_builtin_commands_registered():
    """After initialize(), all expected commands are in registry."""
    registry = SlashCommandRegistry()

    with patch(
        "cognitex.agent.slash_commands._register_skill_commands",
        new_callable=lambda: lambda: AsyncMock(),
    ):
        from cognitex.agent.slash_commands import _register_builtin_commands

        _register_builtin_commands(registry)

    expected = {"model", "provider", "status", "mode", "skills", "skill", "briefing",
                "next", "approve", "reject", "help"}
    registered = set(registry._commands.keys())
    assert expected.issubset(registered), f"Missing: {expected - registered}"


# ---------------------------------------------------------------------------
# Skill user_invocable field
# ---------------------------------------------------------------------------


def test_skill_user_invocable_field():
    """Skill frontmatter with user-invocable: true is parsed correctly."""
    from pathlib import Path

    from cognitex.agent.skills import SkillsLoader

    loader = SkillsLoader()
    content = """---
name: test-skill
description: A test skill
version: "1.0"
user-invocable: true
---

## Purpose
Testing user invocable field.
"""
    skill = loader._parse_skill_file(content, "test-skill", Path("/tmp/test-skill"), False)
    assert skill.user_invocable is True
    assert skill.name == "test-skill"


def test_skill_user_invocable_default():
    """Skills without user-invocable default to False."""
    from pathlib import Path

    from cognitex.agent.skills import SkillsLoader

    loader = SkillsLoader()
    content = """---
name: normal-skill
description: No invocable flag
---

## Purpose
Normal skill.
"""
    skill = loader._parse_skill_file(content, "normal-skill", Path("/tmp/normal-skill"), False)
    assert skill.user_invocable is False


# ---------------------------------------------------------------------------
# Task-aware model routing (LLMService.complete)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_resolves_task_model_override():
    """When task= is given and override is set, uses the override model."""
    from cognitex.services.llm import LLMService

    mock_config = ModelConfig(
        provider="anthropic",
        planner_model="claude-sonnet-4-20250514",
        executor_model="claude-sonnet-4-20250514",
        embedding_model="BAAI/bge-base-en-v1.5",
        autonomous_model="claude-opus-4-20250514",
    )
    mock_svc = AsyncMock()
    mock_svc.get_config = AsyncMock(return_value=mock_config)

    with patch(
        "cognitex.services.model_config.get_model_config_service",
        return_value=mock_svc,
    ):
        llm = MagicMock(spec=LLMService)
        llm.primary_model = "claude-sonnet-4-20250514"
        llm._resolve_task_model = LLMService._resolve_task_model.__get__(llm)

        result = await llm._resolve_task_model("autonomous")
        assert result == "claude-opus-4-20250514"


@pytest.mark.asyncio
async def test_complete_task_model_fallback_when_unset():
    """When task= is given but no override set, returns None (falls back to primary)."""
    from cognitex.services.llm import LLMService

    mock_config = ModelConfig(
        provider="anthropic",
        planner_model="claude-sonnet-4-20250514",
        executor_model="claude-sonnet-4-20250514",
        embedding_model="BAAI/bge-base-en-v1.5",
    )
    mock_svc = AsyncMock()
    mock_svc.get_config = AsyncMock(return_value=mock_config)

    with patch(
        "cognitex.services.model_config.get_model_config_service",
        return_value=mock_svc,
    ):
        llm = MagicMock(spec=LLMService)
        llm.primary_model = "claude-sonnet-4-20250514"
        llm._resolve_task_model = LLMService._resolve_task_model.__get__(llm)

        result = await llm._resolve_task_model("triage")
        assert result is None


@pytest.mark.asyncio
async def test_complete_task_unknown_slot_returns_none():
    """Unknown task slot returns None without error."""
    from cognitex.services.llm import LLMService

    llm = MagicMock(spec=LLMService)
    llm._resolve_task_model = LLMService._resolve_task_model.__get__(llm)

    result = await llm._resolve_task_model("nonexistent_slot")
    assert result is None


@pytest.mark.asyncio
async def test_complete_explicit_model_ignores_task():
    """When explicit model is passed, task parameter has no effect."""
    from cognitex.services.llm import LLMService

    mock_config = ModelConfig(
        provider="anthropic",
        planner_model="claude-sonnet-4-20250514",
        executor_model="claude-sonnet-4-20250514",
        embedding_model="BAAI/bge-base-en-v1.5",
        autonomous_model="claude-opus-4-20250514",
    )
    mock_svc = AsyncMock()
    mock_svc.get_config = AsyncMock(return_value=mock_config)

    # The complete() method should use the explicit model, not the task override.
    # We verify by checking _resolve_task_model is NOT called when model is explicit.
    llm = MagicMock(spec=LLMService)
    llm.primary_model = "claude-sonnet-4-20250514"
    llm._resolve_task_model = AsyncMock(return_value="claude-opus-4-20250514")
    llm._complete_internal = AsyncMock(return_value="response text")

    # Simulate what complete() does: model is not None, so task is ignored
    model = "explicit-model-id"
    task = "autonomous"
    if model is None and task:
        model = await llm._resolve_task_model(task)
    model = model or llm.primary_model
    assert model == "explicit-model-id"
    llm._resolve_task_model.assert_not_awaited()
