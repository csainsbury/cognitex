"""Tests for the clinical data firewall."""

import pytest

from cognitex.services.clinical_firewall import (
    REDACTION_MARKER,
    ClinicalDataFirewall,
    ClinicalScanResult,
    _get_default_patterns,
)


@pytest.fixture
def firewall():
    """Create a firewall with default patterns (no file)."""
    return ClinicalDataFirewall()


@pytest.fixture
def patterns_file(tmp_path):
    """Create a custom patterns file."""
    path = tmp_path / "patterns.txt"
    path.write_text(
        "# Custom Category\n"
        r"\bSECRET_PATTERN\b" + "\n"
    )
    return str(path)


# --- Detection tests ---


class TestScanDetection:
    def test_scan_detects_chi_number(self, firewall):
        result = firewall.scan("Patient CHI 0101201234")
        assert result.is_clinical is True
        assert "Patient Identifiers" in result.matched_categories

    def test_scan_detects_nhs_number(self, firewall):
        result = firewall.scan("NHS number 943 476 5919")
        assert result.is_clinical is True
        assert "Patient Identifiers" in result.matched_categories

    def test_scan_detects_nhs_number_no_spaces(self, firewall):
        result = firewall.scan("NHS number 9434765919")
        assert result.is_clinical is True

    def test_scan_detects_clinical_results(self, firewall):
        result = firewall.scan("HbA1c: 58")
        assert result.is_clinical is True
        assert "Clinical Results" in result.matched_categories

    def test_scan_detects_egfr(self, firewall):
        result = firewall.scan("eGFR = 45")
        assert result.is_clinical is True

    def test_scan_detects_prescribing(self, firewall):
        result = firewall.scan("Increase insulin dose to 20 units")
        assert result.is_clinical is True
        assert "Prescribing" in result.matched_categories

    def test_scan_detects_clinical_urgency(self, firewall):
        result = firewall.scan("MDT discussion required")
        assert result.is_clinical is True
        assert "Clinical Urgency" in result.matched_categories

    def test_scan_detects_datix(self, firewall):
        result = firewall.scan("Please complete the datix report")
        assert result.is_clinical is True

    def test_scan_detects_nhs_systems(self, firewall):
        result = firewall.scan("Please update TrakCare")
        assert result.is_clinical is True
        assert "NHS Systems" in result.matched_categories

    def test_scan_detects_ward(self, firewall):
        result = firewall.scan("Patient on ward 7 bed 3")
        assert result.is_clinical is True
        assert "Ward / Inpatient" in result.matched_categories

    def test_scan_detects_discharge_summary(self, firewall):
        result = firewall.scan("Please complete discharge summary")
        assert result.is_clinical is True

    def test_scan_detects_clinic_letter(self, firewall):
        result = firewall.scan("Draft clinic letter for review")
        assert result.is_clinical is True
        assert "Clinic / Consultation" in result.matched_categories

    def test_scan_detects_diabetes_type(self, firewall):
        result = firewall.scan("Patient with type 1 diabetes")
        assert result.is_clinical is True

    def test_scan_detects_blood_results(self, firewall):
        result = firewall.scan("blood results are back")
        assert result.is_clinical is True

    def test_scan_detects_mrn(self, firewall):
        result = firewall.scan("MRN: 12345")
        assert result.is_clinical is True

    def test_scan_detects_multiple_categories(self, firewall):
        result = firewall.scan("Patient CHI 0101201234 HbA1c: 58 on insulin 20 units")
        assert result.is_clinical is True
        assert len(result.matched_categories) >= 3


class TestScanCleanText:
    def test_scan_clean_text(self, firewall):
        result = firewall.scan("Meeting at 3pm to discuss project plan")
        assert result.is_clinical is False
        assert result.matched_categories == []

    def test_scan_work_email(self, firewall):
        result = firewall.scan("Can you review the Q3 report by Friday?")
        assert result.is_clinical is False

    def test_scan_empty_text(self, firewall):
        result = firewall.scan("")
        assert result.is_clinical is False

    def test_scan_personal_email(self, firewall):
        result = firewall.scan("See you at the pub tonight at 7")
        assert result.is_clinical is False

    def test_scan_code_review(self, firewall):
        result = firewall.scan("The PR looks good, just need to fix the linting errors in app.py")
        assert result.is_clinical is False


# --- Redaction tests ---


class TestFilterText:
    def test_filter_text_redacts_clinical(self, firewall):
        text = "Patient CHI 0101201234 had HbA1c: 58"
        result = firewall.filter_text(text)
        assert "0101201234" not in result
        assert REDACTION_MARKER in result

    def test_filter_text_redacts_prescribing(self, firewall):
        text = "Started on metformin 500 mg twice daily"
        result = firewall.filter_text(text)
        assert "metformin" not in result
        assert "500 mg" not in result
        assert REDACTION_MARKER in result

    def test_filter_text_preserves_non_clinical(self, firewall):
        text = "Meeting at 3pm to discuss project plan"
        result = firewall.filter_text(text)
        assert result == text

    def test_filter_text_empty(self, firewall):
        assert firewall.filter_text("") == ""

    def test_filter_text_redacts_nhs_system(self, firewall):
        text = "Check SCI-Store for the latest results"
        result = firewall.filter_text(text)
        assert "SCI-Store" not in result


# --- filter_email tests ---


class TestFilterEmail:
    def test_filter_email_block_mode_clinical(self, firewall):
        email = {"subject": "HbA1c: 58", "snippet": "Patient results", "body": ""}
        is_clinical, data = firewall.filter_email(email, mode="block")
        assert is_clinical is True
        assert data is email  # Same object, not modified

    def test_filter_email_clean_passthrough(self, firewall):
        email = {
            "subject": "Q3 Report",
            "snippet": "Please review by Friday",
            "body": "",
        }
        is_clinical, data = firewall.filter_email(email, mode="block")
        assert is_clinical is False
        assert data is email

    def test_filter_email_redact_mode(self, firewall):
        email = {
            "subject": "HbA1c: 58 results",
            "snippet": "Patient on metformin",
            "body": "CHI 0101201234",
        }
        is_clinical, data = firewall.filter_email(email, mode="redact")
        assert is_clinical is True
        assert data is not email  # New dict
        assert "0101201234" not in data["body"]
        assert REDACTION_MARKER in data["subject"]

    def test_filter_email_flag_mode(self, firewall):
        email = {"subject": "HbA1c: 58", "snippet": "Results", "body": ""}
        is_clinical, data = firewall.filter_email(email, mode="flag")
        assert is_clinical is True
        assert data is email  # Unmodified


# --- Pattern loading tests ---


class TestPatternLoading:
    def test_patterns_loaded_from_file(self, patterns_file):
        fw = ClinicalDataFirewall(patterns_path=patterns_file)
        result = fw.scan("Found a SECRET_PATTERN here")
        assert result.is_clinical is True
        assert "Custom Category" in result.matched_categories

    def test_fallback_to_default_patterns(self):
        fw = ClinicalDataFirewall(patterns_path="/nonexistent/file.txt")
        # Should still detect clinical content via hardcoded defaults
        result = fw.scan("HbA1c: 58")
        assert result.is_clinical is True

    def test_no_patterns_path_uses_defaults(self):
        fw = ClinicalDataFirewall(patterns_path=None)
        result = fw.scan("Patient on ward 5")
        assert result.is_clinical is True

    def test_initialize_creates_file(self, tmp_path):
        path = tmp_path / "config" / "patterns.txt"
        fw = ClinicalDataFirewall(patterns_path=str(path))
        fw.initialize()
        assert path.is_file()
        assert "Patient Identifiers" in path.read_text()

    def test_initialize_skips_existing(self, patterns_file):
        from pathlib import Path

        fw = ClinicalDataFirewall(patterns_path=patterns_file)
        original_content = Path(patterns_file).read_text()
        fw.initialize()
        assert Path(patterns_file).read_text() == original_content

    def test_default_patterns_are_valid(self):
        """All default patterns should compile without error."""
        import re

        patterns = _get_default_patterns()
        for _category, regexes in patterns.items():
            for regex in regexes:
                re.compile(regex, re.IGNORECASE)  # Should not raise


# --- ClinicalScanResult tests ---


class TestScanResult:
    def test_default_scan_result(self):
        result = ClinicalScanResult(is_clinical=False)
        assert result.matched_categories == []
        assert result.matched_patterns == []
        assert result.sanitised_text is None
        assert result.bypass_action is None


# --- Singleton tests ---


class TestGetFirewall:
    def test_get_firewall_returns_instance(self, monkeypatch, tmp_path):
        """get_firewall() should return a ClinicalDataFirewall instance."""
        import cognitex.services.clinical_firewall as mod

        # Reset singleton
        mod._firewall_instance = None

        # Mock get_settings to use tmp_path
        from unittest.mock import MagicMock

        mock_settings = MagicMock()
        mock_settings.clinical_firewall_patterns_path = str(
            tmp_path / "config" / "patterns.txt"
        )
        monkeypatch.setattr(
            "cognitex.config.get_settings",
            lambda: mock_settings,
        )

        from cognitex.services.clinical_firewall import get_firewall

        fw = get_firewall()
        assert isinstance(fw, ClinicalDataFirewall)

        # Cleanup
        mod._firewall_instance = None
