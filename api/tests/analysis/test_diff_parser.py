from app.analysis.diffs.parser import build_diff_index

DIFF = """\
diff --git a/src/new_module.py b/src/new_module.py
new file mode 100644
index 0000000..3b18e51
--- /dev/null
+++ b/src/new_module.py
@@ -0,0 +1,3 @@
+def greet():
+    return "hi"
+greet()
diff --git a/src/app.py b/src/app.py
index 1234567..89abcde 100644
--- a/src/app.py
+++ b/src/app.py
@@ -10,7 +10,8 @@ def handler():
 ctx_a
 ctx_b
 ctx_c
-old line
+new line
+another new line
 ctx_d
 ctx_e
 ctx_f
@@ -40,6 +41,7 @@ def other():
 ctx_g
 ctx_h
 ctx_i
+late addition
 ctx_j
 ctx_k
 ctx_l
diff --git a/src/old_name.py b/src/renamed.py
similarity index 90%
rename from src/old_name.py
rename to src/renamed.py
index 1111111..2222222 100644
--- a/src/old_name.py
+++ b/src/renamed.py
@@ -1,3 +1,4 @@
 import os
+import sys
 import json
 x = 1
diff --git a/src/gone.py b/src/gone.py
deleted file mode 100644
index 3333333..0000000
--- a/src/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-print("bye")
-print("gone")
diff --git a/assets/logo.png b/assets/logo.png
index 4444444..5555555 100644
Binary files a/assets/logo.png and b/assets/logo.png differ
"""


def test_statuses_and_paths():
    index = build_diff_index(DIFF)
    assert set(index.files) == {
        "src/new_module.py",
        "src/app.py",
        "src/renamed.py",
        "src/gone.py",
        "assets/logo.png",
    }
    assert index.files["src/new_module.py"].status == "added"
    assert index.files["src/app.py"].status == "modified"
    assert index.files["src/app.py"].old_path is None
    assert index.files["src/gone.py"].status == "deleted"
    assert index.files["assets/logo.png"].status == "modified"

    renamed = index.files["src/renamed.py"]
    assert renamed.status == "renamed"
    assert renamed.old_path == "src/old_name.py"


def test_changed_lines():
    index = build_diff_index(DIFF)
    assert index.files["src/new_module.py"].changed_lines == {1, 2, 3}
    assert index.files["src/app.py"].changed_lines == {13, 14, 44}
    assert index.files["src/renamed.py"].changed_lines == {2}
    assert index.files["src/gone.py"].changed_lines == set()
    assert index.files["assets/logo.png"].changed_lines == set()


def test_context_ranges_merge_and_pad():
    index = build_diff_index(DIFF)
    # 13 and 14 pad out to overlapping intervals and merge; 44 stands alone
    assert index.files["src/app.py"].context_ranges == [(10, 17), (41, 47)]
    # padding never pushes the start below line 1
    assert index.files["src/new_module.py"].context_ranges == [(1, 6)]
    assert index.files["src/gone.py"].context_ranges == []
