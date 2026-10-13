"""Mutation check: prove the tests would catch each fix being undone.

    python dev/mutation_check.py            # all mutations
    python dev/mutation_check.py robots     # only those whose name contains "robots"

Each mutation re-breaks one fix, the way a careless future edit might. The
whole test suite then runs against that broken copy, and it must FAIL. A
mutation the suite doesn't notice means a test that can't catch the bug it's
named after -- this build found several that way.

Safety and correctness:
  * Every mutation runs on a throwaway copy in a temp directory; the working
    tree is never modified, even if the run is interrupted.
  * The unmodified copy is checked first. If it doesn't pass, every mutation
    would look "caught" and the results would mean nothing.
  * A mutation must match its target text exactly once, or it's reported as
    NOT APPLIED -- a stale target would otherwise pass silently.
  * Bytecode caching is off. Python decides whether to recompile by file time
    (to the second) and size, so an edit of equal length made within the same
    second can run the OLD code. That produced a false "survived" once.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
IGNORE = shutil.ignore_patterns(".venv", "__pycache__", "*.db", "*.db-wal", "*.db-shm",
                                "*.hunt.lock", "gemini_model.json", ".git", ".env")

M = []   # (name, path, original, replacement)


def mut(name, path, old, new):
    M.append((name, path, old, new))


# Scope: what changed in WilloMotors during the 1.1 work -- the shared
# model selector, the key/token redaction, and which task each AI feature
# uses. The 1.0 review fixes predate this harness and are covered by their
# own regression tests in tests/test_core.py.

# --- model selection (shared module, identical to job-scout's) ------------------
G = "radar/gemini_models.py"
mut("quota-steps-through-versions", G, "    spent = {family_of(m) for m in exhausted} - {None}", "    spent = set()")
mut("bulk-spends-flash-budget", G, 'QUOTA_FALLBACK = {"quality": True, "bulk": False}',
    'QUOTA_FALLBACK = {"quality": True, "bulk": True}')
mut("unusable-models-forgotten", G, '    _block("unusable", model, time.time() + UNUSABLE_SECONDS)', "    _memo.clear()")
mut("no-switch-back-after-reset", G, "        until = min(until, b_until)", "        pass")
mut("no-retry-after-learning", G, "        if new and new != model:\n            model = new",
    "        if False:\n            model = new")
mut("per-minute-treated-as-daily", G, '    if retry is not None and retry <= 120:\n        return "minute", retry',
    '    if False:\n        return "minute", retry')
mut("first-run-state-crash", G, "    except (OSError, ValueError):\n        d = {}",
    "    except (OSError, ValueError):\n        return {}")
mut("old-state-file-ignored", G, '    if "tasks" not in d and d.get("model"):', "    if False:")
mut("versions-compared-as-text", G, '    return tuple(int(p) for p in v.split("."))', "    return v")
mut("gemini-key-in-url", G,
    '    r = requests.post(GENERATE_URL.format(model=model), headers=headers,\n                      json=body, timeout=timeout)\n    if r.status_code in (404, 429):',
    '    r = requests.post(GENERATE_URL.format(model=model) + "?key=" + key, headers={},\n                      json=body, timeout=timeout)\n    if r.status_code in (404, 429):')

# --- bot token redaction -----------------------------------------------------------
mut("api-error-unredacted", "radar/intake.py",
    'print(f"[listen] telegram {method} failed: {redact(exc)}")',
    'print(f"[listen] telegram {method} failed: {exc}")')
mut("photo-download-unredacted", "radar/intake.py",
    "photo download failed: {redact(exc)}", "photo download failed: {exc}")
mut("notifier-unredacted", "radar/notify.py", "unreachable: {redact(exc)}", "unreachable: {exc}")
mut("settings-error-unescaped", "radar/intake.py",
    '    return "⚠ " + html.escape(str(exc), quote=False)', '    return f"⚠ {exc}"')
mut("token-shape-not-redacted", "radar/redact.py",
    "    for rx in _SHAPES:\n        text = rx.sub(\"***\", text)", "    pass")

# --- task wiring ---------------------------------------------------------------------
mut("extraction-on-quality", "radar/ai.py", 'as_json=True, task="bulk")', 'as_json=True, task="quality")')
mut("photos-on-bulk", "radar/ai.py", 'r = self._post(body, 90, "quality")', 'r = self._post(body, 90, "bulk")')


def run_suite(path: str) -> bool:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "GEMINI_API_KEY": "",
           "TG_TOKEN": "", "TG_CHAT": ""}
    r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                       cwd=path, env=env, capture_output=True, text=True, timeout=900)
    return r.returncode == 0


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    todo = [m for m in M if only in m[0]]
    with tempfile.TemporaryDirectory() as tmp:
        base = os.path.join(tmp, "base")
        shutil.copytree(ROOT, base, ignore=IGNORE)
        if not run_suite(base):
            print("The UNMODIFIED project fails its own tests -- fix that first; "
                  "mutation results would be meaningless.")
            return 2
    bad = 0
    for name, path, old, new in todo:
        with tempfile.TemporaryDirectory() as tmp:
            work = os.path.join(tmp, "p")
            shutil.copytree(ROOT, work, ignore=IGNORE)
            target = os.path.join(work, path)
            src = open(target, encoding="utf-8").read()
            n = src.count(old)
            if n != 1:
                print(f"  NOT APPLIED  {name}  (target found {n} times in {path})")
                bad += 1
                continue
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(src.replace(old, new, 1))
            if run_suite(work):
                print(f"  SURVIVED     {name}  -- no test notices this fix being undone")
                bad += 1
            else:
                print(f"  caught       {name}")
    print(f"\n{len(todo) - bad}/{len(todo)} mutations caught")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
