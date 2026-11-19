"""Coding mode's hands: the folder the coding model may work in, the six things it can do there, and the journal that lets
the owner keep or undo a task. Pure Python, no model and no UI (coding_agent.py drives it).

- Sandbox: one folder, picked by the owner. Every path is resolved (symlinks, case) and must stay inside it.
  Umay's own folders are closed (the owner's rule, 2026-11-17: the coding model has no authority over her code): her source
  (found by its files, wherever it is), the running app, her data under %LOCALAPPDATA%\\Umay, and any folder that
  contains one of them (Documents\\Projects). Secrets (.env, keys) are never read or written.
- Tools: list_dir, read_file (a line range), search (text in files), edit_file (an exact old -> new, once),
  create_file, run_tests. Every answer is a short string for the model, capped (the window is 24k tokens).
- Journal: the first time a file is written its original is copied to %LOCALAPPDATA%\\Umay\\coding\\pending\\ (or
  "did not exist" is noted), so the owner can keep the task (tick: the journal is deleted) or undo it (cross: every
  file goes back, created files and the folders made for them go). It is on the disk, so a pending task survives a
  restart, and no new task starts until it is settled. A file the owner changed after the coding model wrote it is saved to
  coding\\rescued\\ before it is put back.
- run_tests is a fixed runner (python unittest / pytest / npm test, picked from the folder), never a command the
  model wrote, no shell, a timeout. What a test run leaves behind is checked against the tree before it: new files
  join the journal (the cross removes them); a file it changed that the coding model did not write is only reported.
"""
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import paths

READ_LINES = 200            # a read_file answer: at most this many lines...
READ_CHARS = 7000           # ...and characters
SEARCH_HITS = 30
LIST_ENTRIES = 120
TEST_SECONDS = 300
TEST_TAIL = 3500            # the end of the output: where the failures are
MAX_FILE_BYTES = 1_000_000  # a bigger file is not a source file the model should read whole
TREE_LIMIT = 20000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "env", "dist", "build", ".pytest_cache",
             ".mypy_cache", ".idea", ".vscode", ".tox", "target", ".next"}
NOISE = {"__pycache__", ".pytest_cache", ".mypy_cache"}          # a test run's leftovers nobody cares about
SECRET_NAMES = {".env", "id_rsa", "id_ed25519", "credentials", "credentials.json", "secrets.json", ".npmrc", ".pypirc"}
SECRET_SUFFIXES = (".pem", ".key", ".pfx", ".p12", ".keystore")
UMAY_MARKERS = ("ai_assistant.py", "memory_cortex.py")           # a folder holding both is Umay's source


class WorkspaceError(Exception):
    """Something the model (or the owner's pick) must be told in a sentence."""


def home():
    custom = os.environ.get("UMAY_CODING_HOME", "").strip()
    return Path(custom) if custom else Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Umay" / "coding"


def _norm(p):
    return os.path.normcase(os.path.realpath(str(p)))


def _inside(child, parent):
    child, parent = _norm(child), _norm(parent)
    return child == parent or child.startswith(parent.rstrip("\\/") + os.sep)


def is_umay_source(folder):
    folder = Path(folder)
    return all((folder / name).is_file() for name in UMAY_MARKERS)


def closed_folders():
    """Folders the coding model may never touch: Umay's source, the running app, her data. Plus UMAY_CODING_CLOSED (;-separated)."""
    found = [paths.app_dir(), Path(__file__).resolve().parent, Path.home() / "Documents" / "Apps" / "Umay",
             Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Umay", Path.home() / ".claude"]
    found += [Path(p) for p in os.environ.get("UMAY_CODING_CLOSED", "").split(";") if p.strip()]
    return found


def why_closed(folder):
    """A sentence when `folder` can't be the coding model's workspace, else None."""
    folder = Path(folder)
    if not folder.is_dir():
        return "That is not a folder."
    if is_umay_source(folder):
        return "That is Umay's own code: the coding model has no authority over it."
    for closed in closed_folders():
        if _inside(folder, closed):
            return f"That is inside {closed}, which is closed to the coding model."
        if _inside(closed, folder):
            return f"That folder contains {closed}, which is closed to the coding model. Pick a project folder inside it."
    if len(Path(os.path.realpath(folder)).parts) <= 2:
        return "A drive's root is too wide: pick a project folder."
    return None


def secret(path):
    p = Path(path)
    return p.name.lower() in SECRET_NAMES or p.name.lower().startswith(".env") or p.suffix.lower() in SECRET_SUFFIXES


def _hash(path):
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


# --- the journal ---------------------------------------------------------------------------------------------

class Journal:
    """What a task changed, on the disk. State: none (no folder), "working" (the coding model is at it), "pending" (done, the
    owner has not ticked or crossed it)."""

    def __init__(self, base=None):
        self.dir = Path(base) if base else home() / "pending"
        self.file = self.dir / "manifest.json"

    def exists(self):
        return self.file.is_file()

    def load(self):
        try:
            return json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _save(self, data):
        _write_json(self.file, data)

    def begin(self, folder, task):
        if self.exists():
            raise WorkspaceError("The last task has not been ticked or crossed yet.")
        self.dir.mkdir(parents=True, exist_ok=True)
        self._save({"folder": str(folder), "task": task, "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "working", "files": {}, "dirs": [], "summary": ""})

    def status(self):
        data = self.load()
        return data["status"] if data else None

    def set_status(self, status, summary=None):
        data = self.load()
        if data is None:
            return
        data["status"] = status
        if summary is not None:
            data["summary"] = summary
        self._save(data)

    def before_write(self, rel, target):
        """Called by every write: the original is copied once, before its first change."""
        data = self.load()
        if data is None:
            raise WorkspaceError("No task is open.")
        if rel in data["files"]:
            return
        entry = {"existed": Path(target).is_file(), "backup": "", "after": ""}
        if entry["existed"]:
            backups = self.dir / "files"
            backups.mkdir(parents=True, exist_ok=True)
            name = f"{len(data['files']):04d}.bak"
            shutil.copy2(target, backups / name)
            entry["backup"] = name
        data["files"][rel] = entry
        self._save(data)

    def made_dirs(self, rels):
        data = self.load()
        if data is not None:
            data["dirs"] += [r for r in rels if r not in data["dirs"]]
            self._save(data)

    def after_write(self, rel, target):
        data = self.load()
        if data is not None and rel in data["files"]:
            data["files"][rel]["after"] = _hash(target) if Path(target).is_file() else ""
            self._save(data)

    def note_new_files(self, folder, rels):
        """Files a test run made: they join the journal as "did not exist", so the cross removes them."""
        data = self.load()
        if data is None:
            return
        for rel in rels:
            target = Path(folder) / rel
            if rel not in data["files"] and target.is_file():
                data["files"][rel] = {"existed": False, "backup": "", "after": _hash(target)}
        self._save(data)

    def changes(self):
        """[(rel, kind, added, removed)] of the files written: kind new / changed / gone; for the owner's summary."""
        data = self.load()
        out = []
        if data is None:
            return out
        folder = Path(data["folder"])
        for rel, entry in data["files"].items():
            target = folder / rel
            now = _read_lines(target) if target.is_file() else None
            before = _read_lines(self.dir / "files" / entry["backup"]) if entry["backup"] else None
            if before is None and now is None:
                continue
            if before is None:
                out.append((rel, "new", len(now), 0))
            elif now is None:
                out.append((rel, "gone", 0, len(before)))
            elif before != now:
                plus = minus = 0
                for line in difflib.ndiff(before, now):
                    plus += line.startswith("+ ")
                    minus += line.startswith("- ")
                out.append((rel, "changed", plus, minus))
        return out

    def diff_text(self, limit=60000):
        """A unified diff of the whole task, for the window."""
        data = self.load()
        if data is None:
            return ""
        folder, parts = Path(data["folder"]), []
        for rel, entry in data["files"].items():
            target = folder / rel
            before = _read_lines(self.dir / "files" / entry["backup"]) if entry["backup"] else []
            now = _read_lines(target) if target.is_file() else []
            if before is None or now is None:
                parts.append(f"(binary file {rel})\n")
                continue
            parts += list(difflib.unified_diff(before, now, f"a/{rel}", f"b/{rel}", lineterm="", n=2))
            parts.append("")
        text = "\n".join(parts)
        return text if len(text) <= limit else text[:limit] + "\n... (cut)"

    def keep(self):
        """The tick: the changes stay, the journal goes."""
        shutil.rmtree(self.dir, ignore_errors=True)

    def revert(self):
        """The cross: every file goes back. Returns {"restored": n, "removed": n, "rescued": [rel]}."""
        data = self.load()
        result = {"restored": 0, "removed": 0, "rescued": []}
        if data is None:
            return result
        folder = Path(data["folder"])
        rescue = home() / "rescued" / time.strftime("%Y%m%d-%H%M%S")
        for rel, entry in data["files"].items():
            target = folder / rel
            if target.is_file() and entry["after"] and _hash(target) != entry["after"]:
                rescue.mkdir(parents=True, exist_ok=True)       # the owner changed it after the coding model did
                copy = rescue / rel.replace("/", "__").replace("\\", "__")
                shutil.copy2(target, copy)
                result["rescued"].append(rel)
            if entry["existed"]:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.dir / "files" / entry["backup"], target)
                result["restored"] += 1
            elif target.is_file():
                target.unlink()
                result["removed"] += 1
        for rel in sorted(data["dirs"], key=len, reverse=True):  # folders the task made, deepest first, if empty
            try:
                (folder / rel).rmdir()
            except OSError:
                pass
        shutil.rmtree(self.dir, ignore_errors=True)
        return result


def _read_lines(path):
    try:
        raw = Path(path).read_bytes()
        if b"\0" in raw[:4096]:
            return None
        return raw.decode("utf-8").replace("\r\n", "\n").split("\n")
    except (OSError, UnicodeDecodeError):
        return None


# --- the workspace -------------------------------------------------------------------------------------------

class Workspace:
    def __init__(self, folder, journal=None):
        problem = why_closed(folder)
        if problem:
            raise WorkspaceError(problem)
        self.root = Path(os.path.realpath(folder))
        self.journal = journal or Journal()

    def resolve(self, rel, must_exist=True):
        """The real path of `rel` (relative to the folder), or a WorkspaceError saying why not."""
        rel = (rel or "").strip().strip('"').replace("\\", "/")
        if not rel:
            raise WorkspaceError("A path is needed.")
        own = self.root.name + "/"
        if rel.lower().startswith(own.lower()) or rel.lower() == own[:-1].lower():
            # the coding model often starts a path with the folder's own name ("proj/a.py"): that is the root itself, unless a
            # real subfolder of that name exists (spike 2026-11-17: it made proj4/proj4/utils.py)
            inner = rel[len(own):] or "."
            if not (self.root / rel.split("/")[0]).is_dir() or not (self.root / rel).exists():
                rel = inner
        target = Path(os.path.realpath(self.root / rel))
        if Path(rel).is_absolute() or not _inside(target, self.root):
            raise WorkspaceError(f"{rel} is outside the folder; only paths inside it are allowed. This is final: do not look for a way around it; finish and say so.")
        if secret(target):
            raise WorkspaceError(f"{rel} is a secrets file: it is never read or written. This is final: do not look for a way around it; finish and say so.")
        for closed in closed_folders():
            if _inside(target, closed):
                raise WorkspaceError(f"{rel} is in a folder that is closed to you. This is final: do not look for a way around it; finish and say so.")
        if must_exist and not target.exists():
            raise WorkspaceError(f"{rel} does not exist.")
        return target

    def rel(self, target):
        return Path(target).relative_to(self.root).as_posix()

    # reading

    def list_dir(self, rel="."):
        target = self.resolve(rel or ".")
        if not target.is_dir():
            raise WorkspaceError(f"{rel} is a file, not a folder.")
        entries = []
        for child in sorted(target.iterdir(), key=lambda c: (c.is_file(), c.name.lower())):
            if child.name in SKIP_DIRS or secret(child):
                continue
            entries.append(child.name + ("/" if child.is_dir() else ""))
        more = len(entries) - LIST_ENTRIES
        shown = entries[:LIST_ENTRIES]
        return (f"{self.rel(target) or '.'}: " + (", ".join(shown) if shown else "(empty)")
                + (f" ... and {more} more" if more > 0 else ""))

    def read_file(self, rel, start=1, end=0):
        target = self.resolve(rel)
        if not target.is_file():
            raise WorkspaceError(f"{rel} is a folder; use list_dir.")
        if target.stat().st_size > MAX_FILE_BYTES:
            raise WorkspaceError(f"{rel} is over {MAX_FILE_BYTES // 1000} KB: search it instead.")
        lines = _read_lines(target)
        if lines is None:
            raise WorkspaceError(f"{rel} is not a text file.")
        total = len(lines)
        start = max(1, int(start or 1))
        end = int(end or 0)
        end = min(total, end if end >= start else start + READ_LINES - 1, start + READ_LINES - 1)
        out, size = [], 0
        for n in range(start, end + 1):
            row = f"{n}: {lines[n - 1]}"
            size += len(row) + 1
            if size > READ_CHARS:
                end = n - 1
                break
            out.append(row)
        tail = f"\n(lines {start}-{end} of {total}{'; ask for the next range' if end < total else ''})"
        return "\n".join(out) + tail

    def search(self, query, rel="."):
        """Case-insensitive text search; a regex when `query` is wrapped in /slashes/."""
        if not (query or "").strip():
            raise WorkspaceError("A query is needed.")
        base = self.resolve(rel or ".")
        pattern = re.compile(query[1:-1] if len(query) > 2 and query[0] == query[-1] == "/" else re.escape(query),
                             re.I)
        hits, files = [], ([base] if base.is_file() else self._walk(base))
        for path in files:
            if len(hits) >= SEARCH_HITS:
                break
            lines = _read_lines(path) if path.stat().st_size <= MAX_FILE_BYTES else None
            for n, line in enumerate(lines or [], 1):
                if pattern.search(line):
                    hits.append(f"{self.rel(path)}:{n}: {line.strip()[:160]}")
                    if len(hits) >= SEARCH_HITS:
                        break
        if not hits:
            return f"No match for {query!r}."
        return "\n".join(hits) + (f"\n(first {SEARCH_HITS} matches; narrow the query or the path)" if len(hits) >= SEARCH_HITS else "")

    def _walk(self, base):
        for dirpath, dirs, names in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(names):
                path = Path(dirpath) / name
                if not secret(path):
                    yield path

    # writing

    def edit_file(self, rel, old, new):
        """Replaces `old` with `new`, once. `old` must appear exactly once, so the model has to be exact."""
        target = self.resolve(rel)
        if not target.is_file():
            raise WorkspaceError(f"{rel} is a folder.")
        if not old:
            raise WorkspaceError("old is empty: to add text, put the line it goes next to in old and repeat it in new.")
        raw = target.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise WorkspaceError(f"{rel} is not a utf-8 text file.") from None
        newline = "\r\n" if "\r\n" in text else "\n"
        text = text.replace("\r\n", "\n")
        old, new = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
        count = text.count(old)
        if count == 0:
            raise WorkspaceError(f"old was not found in {rel}. Copy it exactly from read_file (without the line numbers).")
        if count > 1:
            raise WorkspaceError(f"old appears {count} times in {rel}: add the lines around it so it is unique.")
        self.journal.before_write(self.rel(target), target)
        target.write_bytes(text.replace(old, new, 1).replace("\n", newline).encode("utf-8"))
        self.journal.after_write(self.rel(target), target)
        return f"Edited {self.rel(target)}."

    def create_file(self, rel, content):
        target = self.resolve(rel, must_exist=False)
        if target.exists():
            raise WorkspaceError(f"{rel} already exists: use edit_file.")
        missing, parent = [], target.parent
        while not parent.exists() and _inside(parent, self.root) and _norm(parent) != _norm(self.root):
            missing.append(self.rel(parent))
            parent = parent.parent
        self.journal.before_write(self.rel(target), target)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.journal.made_dirs(missing)
        target.write_text(content, encoding="utf-8", newline="\n")
        self.journal.after_write(self.rel(target), target)
        return f"Created {self.rel(target)} ({content.count(chr(10)) + 1} lines)."

    # tests

    def test_command(self, target=None):
        """The runner for this folder, fixed by what is in it, or None. `target`: one test file (a Path inside the folder)
        for pytest or unittest; npm test always runs everything."""
        root = self.root
        python = shutil.which("python") or (sys.executable if not getattr(sys, "frozen", False) else None)
        if (root / "package.json").is_file():
            try:
                scripts = json.loads((root / "package.json").read_text(encoding="utf-8")).get("scripts", {})
            except (OSError, ValueError):
                scripts = {}
            npm = shutil.which("npm")
            if "test" in scripts and npm:
                return [npm, "test", "--silent"]
        if python and ((root / "tests").is_dir() or (root / "test").is_dir()):
            has_pytest = any((root / n).is_file() for n in ("pytest.ini", "conftest.py", "tox.ini")) or \
                "pytest" in _read_text(root / "pyproject.toml")
            if has_pytest:
                return [python, "-m", "pytest", "-q", "-x", *([self.rel(target)] if target else [])]
            if target:     # one file: discover in its folder with its name (cwd stays the root, so the project's modules import)
                folder = self.rel(target.parent) or "."
                return [python, "-m", "unittest", "discover", "-s", folder, "-p", target.name]
            return [python, "-m", "unittest", "discover", "-s", "tests" if (root / "tests").is_dir() else "test"]
        return None

    def run_tests(self, rel=None):
        """Runs the folder's tests; `rel` = one test file (a model that wants only its own file's result gets it: asked for
        one, the whole suite answered again and again with an old failure that wasn't its own, 40 rounds, 2026-11-18).
        "", "." or a folder = all of them."""
        target = None
        if (rel or "").strip() not in ("", ".", "./"):
            path = self.resolve(rel)
            if path.is_file():
                target = path
        command = self.test_command(target)
        if command is None:
            return "This folder has no tests I know how to run (tests/ with unittest or pytest, or npm test)."
        before = self._tree()
        try:
            done = subprocess.run(command, cwd=str(self.root), capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=TEST_SECONDS, stdin=subprocess.DEVNULL,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            output = (done.stdout or "") + (done.stderr or "")
            head = (f"Tests {'passed' if done.returncode == 0 else 'FAILED'} (exit {done.returncode})"
                    + (f", only {self.rel(target)}." if target and "npm" not in Path(command[0]).stem.lower() else "."))
        except subprocess.TimeoutExpired as e:
            output = ((e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
            head = f"Tests stopped after {TEST_SECONDS} s."
        except OSError as e:
            return f"The tests could not start: {e}"
        output = output.strip()
        if len(output) > TEST_TAIL:
            output = "...\n" + output[-TEST_TAIL:]
        return f"{head}\n{output}".rstrip() + self._after_tests(before)

    def _tree(self):
        seen = {}
        for path in self._walk(self.root):
            try:
                st = path.stat()
            except OSError:
                continue
            seen[self.rel(path)] = (st.st_mtime_ns, st.st_size)
            if len(seen) >= TREE_LIMIT:
                break
        return seen

    def _after_tests(self, before):
        after = self._tree()
        quiet = lambda rel: any(part in NOISE for part in rel.split("/")) or rel.endswith(".pyc")  # noqa: E731
        new = [r for r in after if r not in before and not quiet(r)]
        changed = [r for r in after if r in before and after[r] != before[r] and not quiet(r)
                   and r not in (self.journal.load() or {}).get("files", {})]
        if new:
            self.journal.note_new_files(self.root, new)
        note = ""
        if new:
            note += f"\n[The test run made {len(new)} new file(s): {', '.join(new[:5])}; the cross removes them.]"
        if changed:
            note += (f"\n[The test run changed {len(changed)} file(s) you did not write: {', '.join(changed[:5])}; "
                     f"the cross cannot undo that.]")
        return note


def _read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
