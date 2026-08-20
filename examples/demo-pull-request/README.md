# Demo pull request

The code in this directory is **intentionally flawed**. It exists so DiffLens has
something to review in a live demo, and so the screenshots in the README show real
findings rather than mocked ones.

Nothing here is imported by the application, and it sits outside `api/` and `web/`
so it never reaches CI's linters. Do not copy any of it into real code.

`inventory.py` carries one defect per detector the pipeline ships with:

| Line | What is wrong | Caught by |
| --- | --- | --- |
| unused import | dead import left behind | ruff F401 |
| hardcoded credential | secret pasted into source | ruff S105, detect-secrets |
| `shell=True` | command injection through an f-string | ruff S602 |
| mutable default | shared list across calls | ruff B006 |
| string-built SQL | SQL injection through an f-string | ruff S608 |
| undefined name | function never imported or defined | ruff F821 |
| no test file | new module ships without tests | missing-tests heuristic |

The AI reviewer should add the reasoning the analyzers cannot: why the shared
default accumulates across requests, and why the credential matters once it is in
git history.
