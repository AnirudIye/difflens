"""Smoke tests for the reporting helpers."""

from app.report import fetch_reports


def test_fetch_reports_accepts_owner():
    fetch_reports("alice")
