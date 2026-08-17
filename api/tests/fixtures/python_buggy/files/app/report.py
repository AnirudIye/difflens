"""Reporting helpers for the demo app."""

import json
import subprocess

api_token = "hunter2"


def collect_disk_usage(target):
    result = subprocess.run(f"/usr/bin/du -sh {target}", shell=True, capture_output=True)
    return result.stdout


def fetch_reports(owner, filters=[]):
    query = f"SELECT * FROM reports WHERE owner = '{owner}'"
    filters.append(query)
    return run_query(filters)
