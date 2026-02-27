"""Clinical Data Firewall — Pre-LLM filtering of clinical/NHS content.

Runs regex pattern matching OUTSIDE the LLM to prevent patient identifiers,
clinical results, prescribing data, and ward context from reaching LLM prompts.
This is a hard code gate, not a prompt instruction.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import structlog

logger = structlog.get_logger()

REDACTION_MARKER = "[CLINICAL_REDACTED]"


@dataclass
class ClinicalScanResult:
    """Result of scanning text for clinical content."""

    is_clinical: bool
    matched_categories: list[str] = field(default_factory=list)
    matched_patterns: list[str] = field(default_factory=list)
    sanitised_text: str | None = None
    bypass_action: str | None = None


def _get_default_patterns() -> dict[str, list[str]]:
    """Hardcoded fallback patterns — always available even without config file."""
    return {
        "Patient Identifiers": [
            r"\b\d{10}\b",  # CHI number (10-digit)
            r"\b\d{3}\s?\d{3}\s?\d{4}\b",  # NHS number (3-3-4)
            r"\b(?:hospital|hosp)\s*(?:no|number|#)\s*:?\s*\w+",
            r"\bMRN\s*:?\s*\d+",
        ],
        "Clinical Urgency": [
            r"\b(?:MDT|multi.?disciplinary\s+team)\b",
            r"\b(?:adverse\s+event|datix|safeguard(?:ing)?)\b",
            r"\b(?:clinical\s+incident|never\s+event|duty\s+of\s+candour)\b",
        ],
        "Ward / Inpatient": [
            r"\b(?:ward\s+\d+|bed\s+\d+|bay\s+\d+)\b",
            r"\b(?:discharge\s+(?:summary|letter|planning))\b",
            r"\b(?:inpatient|outpatient|day\s+case)\b",
        ],
        "Clinical Results": [
            r"\b(?:HbA1c|eGFR|creatinine|troponin|CRP|WCC|Hb)\s*[:=]?\s*\d+",
            r"\bblood\s+(?:results?|tests?|gases?)\b",
            r"\b(?:glucose|ketones?|lactate)\s*[:=]?\s*\d+",
        ],
        "Prescribing": [
            r"\b(?:insulin|metformin|gliclazide|empagliflozin|semaglutide)\b",
            r"\b(?:dose|dosage)\s+(?:adjust|change|increas|decreas|titrat)\w*",
            r"\b\d+\s*(?:mg|mcg|units?|iu)\b",
        ],
        "Clinic / Consultation": [
            r"\b(?:clinic\s+letter|referral\s+letter|discharge\s+letter)\b",
            r"\b(?:type\s+[12]\s+diabetes)\b",
            r"\b(?:diabetes\s+(?:clinic|review|follow.?up))\b",
        ],
        "NHS Systems": [
            r"\b(?:SCI.?Store|TrakCare|EMIS|BadgerNet|Vision|SystemOne)\b",
            r"\b(?:SNOMED|ICD.?10|Read\s+code)\b",
        ],
    }


def _patterns_file_content(patterns: dict[str, list[str]]) -> str:
    """Generate the default patterns file content."""
    lines = [
        "# Clinical Data Firewall — Regex Patterns",
        "# Lines starting with # are comments. Blank lines are ignored.",
        "# Sections start with '# Category Name' (must match exactly).",
        "# Each non-comment, non-blank line is a Python regex pattern.",
        "",
    ]
    for category, regexes in patterns.items():
        lines.append(f"# {category}")
        for regex in regexes:
            lines.append(regex)
        lines.append("")
    return "\n".join(lines)


class ClinicalDataFirewall:
    """Pre-LLM firewall that scans text for clinical/NHS content via regex."""

    def __init__(self, patterns_path: str | None = None):
        self._patterns_path = patterns_path
        self._compiled: dict[str, list[re.Pattern]] = {}
        self._load_patterns()

    def _load_patterns(self) -> None:
        """Load regex patterns from file, falling back to hardcoded defaults."""
        patterns: dict[str, list[str]] | None = None

        if self._patterns_path:
            expanded = os.path.expanduser(self._patterns_path)
            if os.path.isfile(expanded):
                patterns = self._parse_patterns_file(expanded)

        if patterns is None:
            patterns = _get_default_patterns()

        # Compile all regexes
        self._compiled = {}
        for category, regexes in patterns.items():
            compiled = []
            for regex in regexes:
                try:
                    compiled.append(re.compile(regex, re.IGNORECASE))
                except re.error as e:
                    logger.warning(
                        "Invalid clinical firewall regex",
                        pattern=regex,
                        category=category,
                        error=str(e),
                    )
            if compiled:
                self._compiled[category] = compiled

    @staticmethod
    def _parse_patterns_file(path: str) -> dict[str, list[str]]:
        """Parse patterns file with '# Category' headers and regex lines."""
        patterns: dict[str, list[str]] = {}
        current_category = "Uncategorised"

        with open(path) as f:
            for line in f:
                line = line.rstrip("\n")
                stripped = line.strip()

                if not stripped:
                    continue

                # Category header: starts with '# ' and the rest is the name
                if stripped.startswith("# "):
                    candidate = stripped[2:].strip()
                    # Skip comment-only lines (contain keywords like 'Firewall', 'Lines', etc.)
                    if candidate and not any(
                        kw in candidate.lower()
                        for kw in ["firewall", "lines starting", "sections start", "each non"]
                    ):
                        current_category = candidate
                        if current_category not in patterns:
                            patterns[current_category] = []
                    continue

                # Skip pure comment lines
                if stripped.startswith("#"):
                    continue

                # Regex pattern line
                if current_category not in patterns:
                    patterns[current_category] = []
                patterns[current_category].append(stripped)

        return patterns

    def initialize(self) -> None:
        """Create default patterns file if it doesn't exist."""
        if not self._patterns_path:
            return

        expanded = Path(os.path.expanduser(self._patterns_path))
        if expanded.exists():
            return

        expanded.parent.mkdir(parents=True, exist_ok=True)
        expanded.write_text(_patterns_file_content(_get_default_patterns()))
        logger.info("Created default clinical firewall patterns file", path=str(expanded))

    def scan(self, text: str) -> ClinicalScanResult:
        """Scan text for clinical content. Returns scan result."""
        if not text:
            return ClinicalScanResult(is_clinical=False)

        matched_categories: list[str] = []
        matched_patterns: list[str] = []

        for category, compiled_list in self._compiled.items():
            for pattern in compiled_list:
                if pattern.search(text):
                    if category not in matched_categories:
                        matched_categories.append(category)
                    matched_patterns.append(pattern.pattern)

        is_clinical = len(matched_categories) > 0

        return ClinicalScanResult(
            is_clinical=is_clinical,
            matched_categories=matched_categories,
            matched_patterns=matched_patterns,
        )

    def filter_text(self, text: str) -> str:
        """Replace matched clinical patterns with redaction marker."""
        if not text:
            return text

        result = text
        for compiled_list in self._compiled.values():
            for pattern in compiled_list:
                result = pattern.sub(REDACTION_MARKER, result)
        return result

    def filter_email(self, email_data: dict, mode: str = "block") -> tuple[bool, dict]:
        """Filter an email dict for clinical content.

        Returns:
            (is_clinical, possibly_modified_email_data)
            - block mode: (True, original_data) — caller should skip LLM
            - redact mode: (True, redacted_copy) — caller processes sanitised version
            - flag mode: (True, original_data) — caller processes with warning
            - clean email: (False, original_data) — no clinical content found
        """
        scan_text = (
            f"{email_data.get('subject', '')} "
            f"{email_data.get('snippet', '')} "
            f"{email_data.get('body', '')}"
        )
        scan_result = self.scan(scan_text)

        if not scan_result.is_clinical:
            return False, email_data

        if mode == "redact":
            redacted = {**email_data}
            for key in ("subject", "snippet", "body"):
                if key in redacted and redacted[key]:
                    redacted[key] = self.filter_text(redacted[key])
            return True, redacted

        # block and flag both return original data
        return True, email_data


# Singleton
_firewall_instance: ClinicalDataFirewall | None = None


def get_firewall() -> ClinicalDataFirewall:
    """Get or create the singleton ClinicalDataFirewall instance."""
    global _firewall_instance
    if _firewall_instance is None:
        from cognitex.config import get_settings

        settings = get_settings()
        _firewall_instance = ClinicalDataFirewall(
            patterns_path=settings.clinical_firewall_patterns_path,
        )
        _firewall_instance.initialize()
    return _firewall_instance
