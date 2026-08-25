# Demo pull request

The code in this directory is **intentionally flawed**. It exists so DiffLens has
something to review in a live demo, and so the screenshots in the README show real
findings rather than mocked ones.

Nothing here is imported by the application, and it sits outside `api/` and `web/`
so it never reaches CI's linters. Do not copy any of it into real code.

There are two files, and the split is the point. `inventory.py` is what the
deterministic analyzers catch. `summaries.py` is what only reading the code
catches: ruff reports nothing on it under any rule set, so every finding there
comes from the AI reviewer.

## inventory.py, for the analyzers

One defect per detector the pipeline ships with:

| Line | What is wrong | Caught by |
| --- | --- | --- |
| unused import | dead import left behind | ruff F401 |
| hardcoded credential | secret pasted into source | ruff S105, detect-secrets |
| `shell=True` | command injection through an f-string | ruff S602 |
| mutable default | shared list across calls | ruff B006 |
| string-built SQL | SQL injection through an f-string | ruff S608 |
| undefined name | function never imported or defined | ruff F821 |
| no test file | new module ships without tests | missing-tests heuristic |

On this file the AI reviewer adds the reasoning the analyzers cannot: why the
shared default accumulates across calls, and why the credential matters once it
is in git history.

## summaries.py, for the AI reviewer

Three logic bugs with no lint signature at all:

| Function | What is wrong | Why a linter cannot see it |
| --- | --- | --- |
| `paginate` | `start + per_page - 1` silently drops the last item of every page | the slice is syntactically fine, only the intent is wrong |
| `cache_key` | ignores `viewer_id`, so one viewer's filtered totals are served to the next viewer | an unused argument is a style nit, the data leak is two functions away |
| `average_amount` | divides by `len(rows)` without guarding the empty case | the division is valid Python, the crash lives in the input |

The cache bug is the interesting one. `summarize_for_viewer` correctly filters
rows by `viewer_id`, then caches the result under a key that does not include
it, so the permission check is real but the memoization throws it away. Catching
that means following a value across three functions, which is the job the
deterministic analyzers do not do.
