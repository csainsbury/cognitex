"""Tests for Gemini schema cleaning and tool schema generation."""

from cognitex.agent.tools import GEMINI_REJECTED_KEYS, _clean_for_gemini


class TestCleanForGemini:
    """Tests for the _clean_for_gemini recursive utility."""

    def test_strips_rejected_keys(self):
        schema = {
            "type": "string",
            "description": "A name",
            "additionalProperties": False,
            "$schema": "http://json-schema.org/draft-07/schema#",
            "default": "foo",
            "title": "Name",
        }
        result = _clean_for_gemini(schema)
        assert result == {"type": "string", "description": "A name"}
        for key in ("additionalProperties", "$schema", "default", "title"):
            assert key not in result

    def test_strips_all_rejected_keys(self):
        """Every key in GEMINI_REJECTED_KEYS should be stripped."""
        schema = dict.fromkeys(GEMINI_REJECTED_KEYS, "value")
        schema["type"] = "string"  # a valid key
        result = _clean_for_gemini(schema)
        # Only 'type' and 'enum' (from const→enum conversion) should remain
        for key in GEMINI_REJECTED_KEYS:
            assert key not in result
        assert result["type"] == "string"

    def test_recurses_into_nested_dicts(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "title": "Name",
                    "default": "",
                },
                "age": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 200,
                },
            },
        }
        result = _clean_for_gemini(schema)
        assert result["properties"]["name"] == {"type": "string"}
        assert result["properties"]["age"] == {"type": "integer"}

    def test_recurses_into_items(self):
        schema = {
            "type": "array",
            "items": {
                "type": "string",
                "minLength": 1,
                "maxLength": 100,
                "pattern": "^[a-z]+$",
            },
        }
        result = _clean_for_gemini(schema)
        assert result["items"] == {"type": "string"}

    def test_converts_const_to_enum(self):
        schema = {
            "type": "string",
            "const": "fixed_value",
        }
        result = _clean_for_gemini(schema)
        assert "const" not in result
        assert result["enum"] == ["fixed_value"]

    def test_preserves_valid_keys(self):
        schema = {
            "type": "string",
            "description": "A description",
            "enum": ["a", "b", "c"],
        }
        result = _clean_for_gemini(schema)
        assert result == schema

    def test_does_not_mutate_original(self):
        schema = {
            "type": "string",
            "title": "Name",
            "properties": {
                "sub": {"type": "integer", "default": 0},
            },
        }
        import copy

        original = copy.deepcopy(schema)
        _clean_for_gemini(schema)
        assert schema == original

    def test_handles_deeply_nested_structures(self):
        schema = {
            "type": "object",
            "properties": {
                "outer": {
                    "type": "object",
                    "title": "Outer",
                    "properties": {
                        "inner": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "string",
                                "format": "date-time",
                            },
                        },
                    },
                },
            },
        }
        result = _clean_for_gemini(schema)
        inner = result["properties"]["outer"]["properties"]["inner"]
        assert inner == {"type": "array", "items": {"type": "string"}}

    def test_handles_list_of_dicts(self):
        schema = {
            "type": "array",
            "items": [
                {"type": "string", "title": "A"},
                {"type": "integer", "default": 0},
            ],
        }
        result = _clean_for_gemini(schema)
        assert result["items"] == [{"type": "string"}, {"type": "integer"}]

    def test_empty_dict(self):
        assert _clean_for_gemini({}) == {}


class TestGeminiRejectedKeys:
    """Verify the frozenset covers all expected keywords."""

    def test_contains_all_known_keywords(self):
        expected = {
            "additionalProperties",
            "$schema",
            "default",
            "title",
            "format",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "minLength",
            "maxLength",
            "pattern",
            "minItems",
            "maxItems",
            "uniqueItems",
            "minProperties",
            "maxProperties",
            "multipleOf",
            "$ref",
            "$defs",
            "$id",
            "definitions",
            "examples",
            "patternProperties",
            "const",
            "deprecated",
        }
        assert expected == GEMINI_REJECTED_KEYS

    def test_is_frozenset(self):
        assert isinstance(GEMINI_REJECTED_KEYS, frozenset)


class TestBaseToolToGeminiSchema:
    """Test BaseTool.to_gemini_schema() end-to-end."""

    def test_basic_tool_schema(self):
        from cognitex.agent.tools import BaseTool, ToolCategory, ToolResult, ToolRisk

        class DummyTool(BaseTool):
            name = "test_tool"
            description = "A test tool."
            risk = ToolRisk.READONLY
            category = ToolCategory.READONLY
            parameters = {
                "query": {"type": "string", "description": "The query"},
                "limit": {
                    "type": "integer",
                    "description": "Max results",
                    "optional": True,
                    "default": 10,
                },
            }

            async def execute(self, **_kwargs) -> ToolResult:
                return ToolResult(success=True)

        tool = DummyTool()
        schema = tool.to_gemini_schema()

        assert schema["name"] == "test_tool"
        assert schema["description"] == "A test tool."
        assert "properties" in schema["parameters"]
        props = schema["parameters"]["properties"]

        # Should NOT contain rejected keys
        assert "default" not in props["limit"]
        assert "optional" not in props["limit"]  # optional is a custom key, not rejected
        assert props["query"]["type"] == "string"
        assert props["query"]["description"] == "The query"
