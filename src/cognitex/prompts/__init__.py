"""
Prompt templates for LLM-based agents.

Prompts are stored as markdown files and loaded at runtime,
allowing easy iteration without code changes.
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent


def load_prompt(name: str) -> str:
    """Load a prompt template by name.

    Args:
        name: Prompt name (without .md extension)

    Returns:
        The prompt template as a string
    """
    prompt_file = PROMPTS_DIR / f"{name}.md"
    if not prompt_file.exists():
        raise FileNotFoundError(f"Prompt not found: {prompt_file}")
    return prompt_file.read_text()


def format_prompt(name: str, **kwargs) -> str:
    """Load and format a prompt template with the given variables.

    Args:
        name: Prompt name (without .md extension)
        **kwargs: Variables to substitute in the template

    Returns:
        The formatted prompt
    """
    template = load_prompt(name)
    return template.format(**kwargs)
