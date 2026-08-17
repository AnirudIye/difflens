"""Adapter over the detect-secrets Python API for changed files of any language."""

from pathlib import Path

from detect_secrets.core.plugins.util import get_mapping_from_secret_type_to_class
from detect_secrets.core.secrets_collection import SecretsCollection
from detect_secrets.settings import default_settings

from app.analysis.analyzers.mappings import secret_confidence
from app.analysis.diffs.parser import DiffIndex
from app.analysis.diffs.validator import is_reviewable, touches_change
from app.analysis.models import Confidence, Finding

_DOWNGRADE: dict[Confidence, Confidence] = {"high": "medium", "medium": "low", "low": "low"}


class SecretsAnalyzer:
    name = "detect-secrets"

    def analyze(self, workspace: Path, index: DiffIndex) -> list[Finding]:
        by_scan_path = {
            str(workspace / path): path
            for path in sorted(index.files)
            if is_reviewable(index, workspace, path)
        }
        if not by_scan_path:
            return []

        collection = SecretsCollection()
        with default_settings():
            for scan_path in by_scan_path:
                collection.scan_file(scan_path)

        detector_classes = get_mapping_from_secret_type_to_class()
        findings: list[Finding] = []
        for scanned, secret in collection:
            path = by_scan_path[scanned]
            line = secret.line_number
            plugin = detector_classes.get(secret.type)
            detector = plugin.__name__ if plugin else secret.type
            confidence = secret_confidence(detector)
            # a secret in a changed file but outside the changed lines is still
            # worth reporting, just with less certainty that this PR added it
            if not touches_change(index, path, line, line):
                confidence = _DOWNGRADE[confidence]
            findings.append(
                Finding(
                    file_path=path,
                    start_line=line,
                    end_line=line,
                    severity="critical",
                    category="security",
                    confidence=confidence,
                    source="deterministic",
                    title=f"Potential {secret.type} committed",
                    # never echo the matched value into the review output
                    explanation=(
                        f"Line {line} of {path} matches the {secret.type} pattern. "
                        "The matched value is deliberately omitted from this report."
                    ),
                    recommendation="Rotate this credential and purge it from git history",
                    rule_id=detector,
                    tool="detect-secrets",
                )
            )
        findings.sort(key=lambda f: (f.file_path, f.start_line or 0, f.rule_id or ""))
        return findings
