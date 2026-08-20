"""Intentionally flawed sample module used to demo the DiffLens reviewer.

Not imported anywhere, not covered by CI. Every defect below is deliberate.
See README.md in this directory for the full list. Do not copy this code.
"""

import json
import subprocess

# Amazon's documented example key: a placeholder, never a live credential
api_token = "AKIAIOSFODNN7EXAMPLE"


def disk_usage(path):
    result = subprocess.run(f"/usr/bin/du -sh {path}", shell=True, capture_output=True)
    return result.stdout


def find_items(owner, seen=[]):
    query = f"SELECT * FROM inventory WHERE owner = '{owner}'"
    seen.append(query)
    return run_query(seen)
