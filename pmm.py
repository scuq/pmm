#!/usr/bin/env python3
"""pmm - project memory manager.

pmm reads and maintains the Markdown records in .project/:

    .project/NOW.md        current focus, next steps, blockers
    .project/features/     F001-<slug>.md
    .project/decisions/    ADR-001-<slug>.md
    .project/phases/       P01-<slug>.md
    .project/ideas/        I001-<slug>.md
    .project/pmm           a copy of this script

pmm also decides where a new record goes: "pmm where WORDS" answers with
the kind, the path and the command that creates the record.

Each record starts with a flat frontmatter block. Each line is
"key: value". The keys "tags", "decisions" and "files" hold a list on one
line, for example "tags: [auth, api]". No other YAML syntax is supported.

"files" names the source files or directories a feature or decision
governs, relative to the repository root. "pmm about PATH" finds the
records for a path before the code is changed.

The frontmatter is the index. There is no separate index file.

pmm uses only the Python standard library. It needs Python 3.9 or later.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import date
from pathlib import Path

__version__ = "0.3.0"

# GitHub repository that publishes releases of this script, as "owner/name".
# Self-update is disabled while the owner is the placeholder "OWNER".
UPDATE_REPO = "scuq/pmm"
ASSET_NAME = "pmm.py"

UPDATE_INTERVAL_S = 24 * 60 * 60
AUTO_TIMEOUT_S = 3
MANUAL_TIMEOUT_S = 15
MAX_SCRIPT_BYTES = 1024 * 1024
MAX_DOC_BYTES = 256 * 1024
MAX_FRONTMATTER_LINES = 60
MAX_WALK_UP = 40
MAX_SEARCH_LINES = 3
MAX_SEARCH_FILES = 20
MAX_LOG_LINES = 20
MAX_NOW_LINES = 40
MAX_RELATED = 10

PROJECT_DIR = ".project"
SCRIPT_NAME = "pmm"
NOW_NAME = "NOW.md"
STAMP_NAME = "pmm-last-check"
CONFIG_NAME = "config"
MAX_CAT_BYTES = 64 * 1024

LIST_KEYS = ("tags", "decisions", "files")

# "list" hides records in these states unless --all or --status is given.
CLOSED = ("done", "dropped", "rejected", "superseded", "promoted")

KINDS = {
    "feature": {
        "dir": "features",
        "prefix": "F",
        "width": 3,
        "status": ("planned", "in_progress", "done", "dropped"),
        "keys": ("id", "title", "status", "phase", "tags", "decisions", "files"),
        "sections": ("Goal", "Requirements", "Acceptance"),
    },
    "decision": {
        "dir": "decisions",
        "prefix": "ADR-",
        "width": 3,
        "status": ("proposed", "accepted", "superseded", "rejected"),
        "keys": ("id", "title", "status", "date", "tags", "superseded_by", "files"),
        "sections": ("Context", "Decision", "Consequences"),
    },
    "phase": {
        "dir": "phases",
        "prefix": "P",
        "width": 2,
        "status": ("planned", "in_progress", "done"),
        "keys": ("id", "title", "status"),
        "sections": ("Goal", "Exit criteria"),
    },
    "idea": {
        "dir": "ideas",
        "prefix": "I",
        "width": 3,
        "status": ("open", "promoted", "dropped"),
        "keys": ("id", "title", "status", "tags", "promoted_to"),
        "sections": ("Idea", "Why"),
    },
}

ID_RE = re.compile(r"^(ADR-\d{3,}|F\d{3,}|P\d{2,}|I\d{3,})$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VERSION_RE = re.compile(r'^__version__ = "(\d+)\.(\d+)\.(\d+)"\s*$', re.MULTILINE)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

NOW_TEMPLATE = "# Now\n\nFocus:\n\nNext:\n\nBlockers:\n"
CONFIG_TEMPLATE = (
    "# pmm settings, one \"key: value\" per line.\n"
    "# test: the command that \"pmm done\" runs before it commits.\n"
    "# read: files that every task must know, comma-separated; brief prints them.\n"
    "test: \n"
    "read: %s\n"
)
# Files that every task should know, when they exist. init puts them in "read:".
ALWAYS_READ_CANDIDATES = ("README.md", "CONTRIBUTING.md", "docs/style.md", "STYLE.md")

# Where a new piece of knowledge goes. "pmm where WORDS" matches the words
# against "words" and prints the best place. The order is the order of the
# full table that "pmm where" prints without arguments.
PLACES = (
    {
        "name": "idea",
        "path": ".project/ideas/I###-<slug>.md",
        "use": "a thought that is not planned work yet; cheap to add, cheap to drop",
        "create": "pmm new idea \"Title\" [--tags a,b]",
        "then": "pmm set I### status=promoted promoted_to=F### (or ADR-###); or status=dropped",
        "words": ("idea", "thought", "suggestion", "proposal", "maybe", "someday", "later",
                  "wish", "brainstorm", "could", "option", "possibility", "experiment"),
    },
    {
        "name": "feature",
        "path": ".project/features/F###-<slug>.md",
        "use": "a unit of work with a goal, requirements and acceptance criteria",
        "create": "pmm new feature \"Title\" [--phase P##] [--tags a,b] [--decisions ADR-###]",
        "then": "pmm set F### status=in_progress|done|dropped",
        "words": ("feature", "story", "requirement", "capability", "functionality", "epic",
                  "implement", "build", "add", "support", "bug", "fix", "enhancement",
                  "improvement", "change", "work", "deliverable"),
    },
    {
        "name": "decision",
        "path": ".project/decisions/ADR-###-<slug>.md",
        "use": "a choice that changes future work: context, decision, consequences",
        "create": "pmm new decision \"Title\" [--tags a,b]",
        "then": "pmm set ADR-### status=accepted|rejected; replace with status=superseded superseded_by=ADR-###",
        "words": ("decision", "decide", "decided", "chose", "choice", "choose", "adr",
                  "architecture", "tradeoff", "trade-off", "rationale", "why", "alternative",
                  "library", "framework", "stack", "convention", "standard", "policy", "rule",
                  "agreed", "agreement", "constraint"),
    },
    {
        "name": "phase",
        "path": ".project/phases/P##-<slug>.md",
        "use": "a stage of the project with a goal and exit criteria; features point at it",
        "create": "pmm new phase \"Title\"",
        "then": "pmm set P## status=in_progress|done; pmm set F### phase=P##",
        "words": ("phase", "milestone", "sprint", "release", "stage", "iteration", "roadmap",
                  "timeline", "deadline", "mvp", "version", "epoch", "quarter"),
    },
    {
        "name": "now",
        "path": ".project/NOW.md",
        "use": "the current focus, the next steps and the blockers; short, replaced often",
        "create": "edit the file: sections Focus, Next, Blockers",
        "then": "remove lines that are done; a step that grows needs a feature",
        "words": ("now", "focus", "next", "blocker", "blocked", "todo", "task", "current",
                  "wip", "step", "progress", "doing", "working", "status"),
    },
    {
        "name": "nowhere",
        "path": "(not stored)",
        "use": "a session journal, a log of what was done, a meeting note",
        "create": "do not create a record; the commit message holds it",
        "then": "if it changed future work, it is a decision; if it is open work, it is a feature",
        "words": ("journal", "log", "diary", "history", "changelog", "session", "summary",
                  "note", "notes", "meeting", "done", "did", "recap", "report"),
    },
    {
        "name": "outside",
        "path": "README.md or docs/",
        "use": "how to install, configure or use the software; reference material",
        "create": "not project memory; write it in the repository documentation",
        "then": "link to it from a record only if a record depends on it",
        "words": ("readme", "doc", "docs", "documentation", "howto", "setup", "install",
                  "config", "configuration", "usage", "manual", "reference", "api",
                  "tutorial", "guide"),
    },
)

AGENTS_SNIPPET = """\
## Project memory

Project knowledge is in `.project/`. Below, `pmm` means `python3 .project/pmm`.
Query and change the records only through pmm. Read a file in `.project/`
directly only when pmm cannot answer.

- Start: `pmm brief ID --files`, one call; words of the title work instead
  of the id. It returns the record, its decisions, phase, related records,
  the focus, and the files to read with their contents. Do not search for
  the record or read those files again.
- Then make every edit, code and docs alike, in one call before you run
  anything. README changes belong in that same call.
- Finish a feature: `pmm done ID "acceptance text"`, one call. It runs the
  tests, writes Acceptance, sets done, runs check and commits as "ID: title".
- No record id yet: `pmm status`, then `pmm search TERM` or `pmm list`.
- The next planned task: `pmm focus`. Switch: `pmm focus ID`. `done` moves
  the focus to the first id under Next by itself.
- A file that brief did not name: `pmm about PATH` before you change it.
- New knowledge: run `pmm where WORDS` first, then the create command it
  prints. pmm decides the place. Do not choose or create a file yourself.
- Frontmatter: `pmm set ID key=value`. Body text: `pmm write ID Section "text"`.
  Do not edit record files by hand.
- Other commits: `pmm check` first. Commits of a record: `pmm log ID`.

Rules
- Put the record id in each commit message that works on it: "F012: add login form".
- A feature names the files it touches: `pmm set F012 files=src/a.py,src/b/`.
- A feature is done only when its Acceptance section is written and met.
- Record a decision only when it changes future work. Fill Context, Decision
  and Consequences. Never edit an accepted decision; supersede it.
- Keep NOW.md under %d lines. No session journal. Git holds the history.
- When the work is done, report in at most three lines.
- Single words from the user: "next" means run `pmm brief --files` and do
  that task. "done" means `pmm done ID`. "status" means `pmm board`.
""" % MAX_NOW_LINES


# Claude Code hooks for .claude/settings.json. SessionStart puts the
# status into the context. Stop refuses to end a turn while check fails.
HOOKS_SNIPPET = {
    "hooks": {
        "SessionStart": [{
            "matcher": "startup|resume|compact",
            "hooks": [{"type": "command",
                       "command": "[ -f .project/pmm ] && python3 .project/pmm brief || true"}],
        }],
        "Stop": [{
            "hooks": [{"type": "command",
                       "command": "[ -f .project/pmm ] || exit 0; python3 .project/pmm check --hook"}],
        }],
    },
}


SKILL_PATH = ".claude/skills/pmm/SKILL.md"
SETTINGS_PATH = ".claude/settings.json"
AGENT_FILES = ("CLAUDE.md", "AGENTS.md")
BLOCK_MARK = "## Project memory"

# A Claude Code skill for explicit commands: /pmm ID, /pmm new WORDS.
# It is user-only, so it costs no context in sessions that do not use it.
SKILL_TEXT = """\
---
name: pmm
description: Work on a project memory record. /pmm ID or title words implements it. /pmm new WORDS records new knowledge. /pmm alone takes the planned focus.
argument-hint: "[ID | title words | new WORDS]"
disable-model-invocation: true
---

`pmm` means `python3 .project/pmm`. Arguments: $ARGUMENTS

If the arguments start with `new`, go to step 5. Otherwise:

1. Run `pmm brief $ARGUMENTS --files` (with no arguments it takes the focus).
   If it reports several matches, pick the one the user meant and run it again with the id.
2. Read the output once. Do not read records or the listed files again.
3. Make every edit, code and docs alike, in one call. Run nothing before all edits are done.
4. Finish. A feature: `pmm done ID "acceptance text"`. A decision: `pmm set ID status=accepted`.
   An idea: `pmm set ID status=promoted promoted_to=F###` after the feature exists.
   Report in three lines and stop.

5. New knowledge: run `pmm where WORDS` with the words after `new`.
   Run the create command it prints: a title of at most eight words, one `-s Name=text` per section,
   and `--files` for a feature. If this is the next task, run `pmm focus ID`.
   Report the id and the path in one line.
"""


class PmmError(Exception):
    """An error that pmm reports to the user without a traceback."""


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


def find_up(start: Path, name: str) -> Path | None:
    """Return the nearest directory at or above start that contains name."""
    current = start.resolve()
    for _ in range(MAX_WALK_UP):
        if (current / name).exists():
            return current
        if current.parent == current:
            return None
        current = current.parent
    return None


def find_root() -> Path:
    """Return the directory that contains .project/."""
    current = Path.cwd().resolve()
    for _ in range(MAX_WALK_UP):
        if (current / PROJECT_DIR).is_dir():
            return current
        if current.parent == current:
            break
        current = current.parent
    raise PmmError("no %s/ found here or above. Run: pmm init" % PROJECT_DIR)


def find_git_dir(root: Path) -> Path | None:
    """Return the Git directory for root. Return None if there is none.

    In a linked worktree or a submodule, .git is a file that points to
    the Git directory.
    """
    top = find_up(root, ".git")
    if top is None:
        return None
    dot_git = top / ".git"
    if dot_git.is_dir():
        return dot_git
    try:
        text = dot_git.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    first = text.splitlines()[0] if text else ""
    if not first.startswith("gitdir:"):
        return None
    target = Path(first[len("gitdir:"):].strip())
    if not target.is_absolute():
        target = top / target
    return target if target.is_dir() else None


def read_config(root: Path) -> dict:
    """Read .project/config: "key: value" lines, "#" starts a comment."""
    path = root / PROJECT_DIR / CONFIG_NAME
    result = {}
    if not path.is_file():
        return result
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, colon, value = line.partition(":")
        if colon and key.strip():
            result[key.strip()] = value.strip()
    return result


def rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------


def kind_of_id(record_id: str) -> str | None:
    if not ID_RE.match(record_id):
        return None
    if record_id.startswith("ADR-"):
        return "decision"
    if record_id.startswith("F"):
        return "feature"
    if record_id.startswith("I"):
        return "idea"
    return "phase"


def parse_list(value: str) -> list[str]:
    """Parse "[a, b]" or "a, b" into a list of items."""
    text = value.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    items = []
    for part in text.split(","):
        item = part.strip().strip("\"'")
        if item:
            items.append(item)
    return items


def format_value(key: str, value) -> str:
    if key in LIST_KEYS:
        return "[" + ", ".join(value) + "]"
    return str(value)


def read_text(path: Path) -> str:
    with open(path, "rb") as handle:
        data = handle.read(MAX_DOC_BYTES + 1)
    if len(data) > MAX_DOC_BYTES:
        raise PmmError("%s is larger than %d bytes" % (path, MAX_DOC_BYTES))
    return data.decode("utf-8", errors="replace")


def parse_record(path: Path, kind: str, root: Path) -> dict:
    """Read one record. Parse problems go into record["problems"]."""
    record = {"path": path, "kind": kind, "meta": {}, "body": "", "problems": [], "root": root}
    lines = read_text(path).splitlines()
    if not lines or lines[0].strip() != "---":
        record["problems"].append("no frontmatter block at the top of the file")
        record["body"] = "\n".join(lines)
        return record
    end = -1
    for index in range(1, min(len(lines), MAX_FRONTMATTER_LINES)):
        if lines[index].strip() == "---":
            end = index
            break
    if end < 0:
        record["problems"].append("frontmatter block is not closed with ---")
        return record
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, colon, value = stripped.partition(":")
        key = key.strip()
        if not colon or not key:
            record["problems"].append("frontmatter line is not 'key: value': %s" % stripped)
            continue
        if key in record["meta"]:
            record["problems"].append("frontmatter key occurs twice: %s" % key)
            continue
        value = value.strip()
        if key in LIST_KEYS:
            record["meta"][key] = parse_list(value)
        else:
            record["meta"][key] = value.strip("\"'") if len(value) > 1 else value
    record["body"] = "\n".join(lines[end + 1:]).strip("\n")
    return record


def load_records(root: Path) -> list[dict]:
    records = []
    for kind, spec in KINDS.items():
        directory = root / PROJECT_DIR / spec["dir"]
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            records.append(parse_record(path, kind, root))
    return records


def index_by_id(records: list[dict]) -> dict:
    """Map id to record. The first record with an id wins."""
    index = {}
    for record in records:
        record_id = record["meta"].get("id", "")
        if record_id and record_id not in index:
            index[record_id] = record
    return index


def get_record(index: dict, record_id: str) -> dict:
    if kind_of_id(record_id) is None:
        raise PmmError("not a valid id: %s (use F001, ADR-001, P01 or I001)" % record_id)
    record = index.get(record_id)
    if record is None:
        raise PmmError("no record with id %s" % record_id)
    return record


def sections_of(body: str) -> dict:
    """Map each "## Heading" in body to the text below it."""
    result = {}
    current = None
    for line in body.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            result[current] = []
        elif current is not None:
            result[current].append(line)
    return {name: "\n".join(lines).strip() for name, lines in result.items()}


def validate(record: dict, index: dict, counts: dict) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for one record."""
    errors = list(record["problems"])
    warnings = []
    meta = record["meta"]
    if errors and not meta:
        # Without a frontmatter block, the other checks only add noise.
        return errors, warnings
    spec = KINDS[record["kind"]]
    record_id = meta.get("id", "")

    if not record_id:
        errors.append("id is missing")
    elif kind_of_id(record_id) != record["kind"]:
        errors.append("id %s is not valid for %s/" % (record_id, spec["dir"]))
    else:
        stem = record["path"].stem
        if stem != record_id and not stem.startswith(record_id + "-"):
            errors.append("file name does not start with the id %s" % record_id)
        if counts.get(record_id, 0) > 1:
            errors.append("id %s is used by more than one file" % record_id)

    if not meta.get("title"):
        errors.append("title is missing")
    status = meta.get("status", "")
    if status not in spec["status"]:
        errors.append("status '%s' is not one of: %s" % (status, ", ".join(spec["status"])))
    for key in meta:
        if key not in spec["keys"]:
            warnings.append("unknown key: %s" % key)

    if record["kind"] == "feature":
        phase = meta.get("phase", "")
        if phase and (kind_of_id(phase) != "phase" or phase not in index):
            errors.append("phase %s does not exist" % phase)
        for decision_id in meta.get("decisions", []):
            target = index.get(decision_id)
            if kind_of_id(decision_id) != "decision" or target is None:
                errors.append("decision %s does not exist" % decision_id)
                continue
            target_status = target["meta"].get("status", "")
            if target_status in ("superseded", "rejected") and status in ("planned", "in_progress"):
                warnings.append("links to %s, which is %s" % (decision_id, target_status))

    if record["kind"] == "decision":
        replacement = meta.get("superseded_by", "")
        if status == "superseded" and not replacement:
            errors.append("status is superseded but superseded_by is empty")
        if replacement:
            if status != "superseded":
                errors.append("superseded_by is set but status is '%s'" % status)
            if replacement == record_id:
                errors.append("superseded_by points to the record itself")
            elif kind_of_id(replacement) != "decision" or replacement not in index:
                errors.append("superseded_by %s does not exist" % replacement)
        day = meta.get("date", "")
        if day and not DATE_RE.match(day):
            errors.append("date '%s' is not YYYY-MM-DD" % day)

    for entry in meta.get("files", []):
        if entry.startswith("/") or entry.startswith("../") or "/../" in entry:
            errors.append("files entry is not relative to the repository: %s" % entry)
        elif not (record["root"] / entry).exists():
            warnings.append("files entry does not exist: %s" % entry)

    # Quality gates. A closed record must say what was decided or accepted.
    sections = sections_of(record["body"])
    if record["kind"] == "feature" and status == "done" and not sections.get("Acceptance"):
        errors.append("status is done but the Acceptance section is empty")
    if record["kind"] == "feature" and status in ("in_progress", "done") and not sections.get("Goal"):
        warnings.append("Goal section is empty")
    if record["kind"] == "decision" and status in ("accepted", "superseded"):
        for name in ("Context", "Decision", "Consequences"):
            if not sections.get(name):
                warnings.append("%s section is empty" % name)

    if record["kind"] == "idea":
        target_id = meta.get("promoted_to", "")
        if status == "promoted" and not target_id:
            errors.append("status is promoted but promoted_to is empty")
        if target_id:
            if status != "promoted":
                errors.append("promoted_to is set but status is '%s'" % status)
            if kind_of_id(target_id) not in ("feature", "decision") or target_id not in index:
                errors.append("promoted_to %s is not an existing feature or decision" % target_id)

    return errors, warnings


# --------------------------------------------------------------------------
# File writes
# --------------------------------------------------------------------------


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    """Write data to path through a temporary file in the same directory."""
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".pmm-tmp-")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, str(path))
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def render_record(kind: str, meta: dict, body: str) -> str:
    lines = ["---"]
    for key in KINDS[kind]["keys"]:
        if key in meta:
            lines.append(("%s: %s" % (key, format_value(key, meta[key]))).rstrip())
    for key in meta:
        if key not in KINDS[kind]["keys"]:
            lines.append(("%s: %s" % (key, format_value(key, meta[key]))).rstrip())
    lines.append("---")
    lines.append("")
    lines.append(body.strip("\n"))
    return "\n".join(lines).rstrip("\n") + "\n"


def write_record(record: dict) -> None:
    text = render_record(record["kind"], record["meta"], record["body"])
    atomic_write(record["path"], text.encode("utf-8"), 0o644)


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------


def summary_line(record: dict) -> str:
    meta = record["meta"]
    parts = [meta.get("id", "?"), meta.get("status", "?")]
    if record["kind"] == "feature":
        parts.append(meta.get("phase", "") or "-")
    parts.append(meta.get("title", ""))
    tags = meta.get("tags", [])
    if tags:
        parts.append("[" + ", ".join(tags) + "]")
    return "  ".join(parts)


def block(record: dict) -> str:
    """Return one record as a header line and its body."""
    meta = record["meta"]
    header = [meta.get("id", "?"), meta.get("title", ""), meta.get("status", "?")]
    if meta.get("phase"):
        header.append("phase " + meta["phase"])
    if meta.get("date"):
        header.append(meta["date"])
    if meta.get("tags"):
        header.append("tags " + ", ".join(meta["tags"]))
    if meta.get("files"):
        header.append("files " + ", ".join(meta["files"]))
    text = "== " + " | ".join(header) + " =="
    if record["body"]:
        text += "\n" + record["body"]
    return text


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def detect_test(root: Path) -> str:
    """Return a test command for the files in root, or an empty string."""
    if (root / "go.mod").is_file():
        return "go test ./..."
    if (root / "Cargo.toml").is_file():
        return "cargo test"
    package = root / "package.json"
    if package.is_file():
        try:
            if json.loads(read_text(package)).get("scripts", {}).get("test"):
                return "npm test"
        except (ValueError, AttributeError):
            pass
    if (root / "pytest.ini").is_file() or (root / "conftest.py").is_file():
        return "python3 -m pytest -q"
    if list((root / "tests").glob("test_*.py")) or list(root.glob("test_*.py")):
        return "python3 -m unittest -q"
    makefile = root / "Makefile"
    if makefile.is_file() and re.search(r"^test:", read_text(makefile), re.M):
        return "make test"
    return ""


def merge_hooks(path: Path) -> bool:
    """Add the pmm hooks to a Claude Code settings file. Return True on change."""
    data = {}
    if path.is_file():
        try:
            data = json.loads(read_text(path))
        except ValueError:
            raise PmmError("%s is not valid JSON. Add the hooks by hand: pmm instruct --hooks" % path) from None
        if not isinstance(data, dict):
            raise PmmError("%s does not hold a JSON object" % path)
    hooks = data.setdefault("hooks", {})
    changed = False
    for event, groups in HOOKS_SNIPPET["hooks"].items():
        existing = hooks.setdefault(event, [])
        present = any(".project/pmm" in hook.get("command", "")
                      for group in existing for hook in group.get("hooks", []))
        if not present:
            existing.extend(groups)
            changed = True
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, (json.dumps(data, indent=2) + "\n").encode("utf-8"), 0o644)
    return changed


def cmd_init(args) -> int:
    top = find_up(Path.cwd(), ".git")
    root = top if top is not None else Path.cwd().resolve()
    project = root / PROJECT_DIR
    project.mkdir(exist_ok=True)
    for spec in KINDS.values():
        (project / spec["dir"]).mkdir(exist_ok=True)
    now = project / NOW_NAME
    if not now.exists():
        atomic_write(now, NOW_TEMPLATE.encode("utf-8"), 0o644)
    config = project / CONFIG_NAME
    if not config.exists():
        found = [name for name in ALWAYS_READ_CANDIDATES if (root / name).is_file()]
        text = CONFIG_TEMPLATE % ", ".join(found)
        test = detect_test(root)
        text = text.replace("test: \n", "test: %s\n" % test)
        atomic_write(config, text.encode("utf-8"), 0o644)
        print("test command: %s" % (test or "none found, set 'test:' in %s/%s" % (PROJECT_DIR, CONFIG_NAME)))

    source = Path(__file__).resolve()
    target = project / SCRIPT_NAME
    if source != target.resolve():
        if target.exists() and not args.force:
            print("kept existing %s (use --force to replace it)" % rel(target, root))
        else:
            atomic_write(target, source.read_bytes(), 0o755)
            print("copied pmm %s to %s" % (__version__, rel(target, root)))
    print("initialized %s" % rel(project, root))
    if args.bare:
        print("Agent files skipped. The block: pmm instruct")
        return 0

    # The instruction block goes into the first agent file that exists.
    # Without one, CLAUDE.md is created.
    agent = next((root / name for name in AGENT_FILES if (root / name).is_file()), None)
    if agent is None:
        agent = root / AGENT_FILES[0]
        atomic_write(agent, ("# %s\n\n%s" % (root.name, AGENTS_SNIPPET)).encode("utf-8"), 0o644)
        print("created %s with the project memory block" % agent.name)
    elif BLOCK_MARK not in read_text(agent):
        text = read_text(agent).rstrip("\n") + "\n\n" + AGENTS_SNIPPET
        atomic_write(agent, text.encode("utf-8"), 0o644)
        print("added the project memory block to %s" % agent.name)
    else:
        print("%s has the project memory block already" % agent.name)

    if agent.name == "CLAUDE.md" or (root / ".claude").is_dir():
        skill = root / SKILL_PATH
        if not (skill.is_file() and read_text(skill) == SKILL_TEXT):
            skill.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(skill, SKILL_TEXT.encode("utf-8"), 0o644)
            print("wrote %s (/pmm command)" % SKILL_PATH)
        if merge_hooks(root / SETTINGS_PATH):
            print("added hooks to %s" % SETTINGS_PATH)
    print("Next: pmm add \"first task\" --now, then commit %s, %s and .claude/" % (PROJECT_DIR, agent.name))
    return 0


def cmd_status(args) -> int:
    root = find_root()
    records = load_records(root)
    groups = (
        ("Phase in progress", "phase", "in_progress"),
        ("Features in progress", "feature", "in_progress"),
        ("Decisions proposed", "decision", "proposed"),
    )
    for title, kind, status in groups:
        matches = [r for r in records if r["kind"] == kind and r["meta"].get("status") == status]
        if not matches:
            continue
        print(title + ":")
        for record in matches:
            print("  " + summary_line(record))
    now = root / PROJECT_DIR / NOW_NAME
    if now.is_file():
        print("--- %s ---" % NOW_NAME)
        print(read_text(now).strip("\n"))
    return 0


def normalize_kind(name: str) -> str:
    kind = name[:-1] if name.endswith("s") else name
    if kind not in KINDS:
        raise PmmError("unknown kind: %s (use feature, decision, phase or idea)" % name)
    return kind


def cmd_list(args) -> int:
    root = find_root()
    records = load_records(root)
    kind = normalize_kind(args.kind) if args.kind else None
    count = 0
    hidden = 0
    for record in records:
        meta = record["meta"]
        if kind and record["kind"] != kind:
            continue
        if args.status and meta.get("status") != args.status:
            continue
        if not args.all and not args.status and meta.get("status") in CLOSED:
            hidden += 1
            continue
        if args.tag and args.tag not in meta.get("tags", []):
            continue
        if args.phase and meta.get("phase") != args.phase:
            continue
        print(summary_line(record))
        count += 1
    if hidden:
        print("(%d closed records hidden; --all shows them)" % hidden)
    if count == 0:
        print("no records match", file=sys.stderr)
    return 0


def cmd_show(args) -> int:
    root = find_root()
    record = get_record(index_by_id(load_records(root)), args.id)
    print(read_text(record["path"]).rstrip("\n"))
    return 0


def cmd_context(args) -> int:
    root = find_root()
    records = load_records(root)
    index = index_by_id(records)
    record = get_record(index, args.id)
    meta = record["meta"]
    record_id = meta["id"]
    blocks = [block(record)]

    if record["kind"] == "feature":
        seen = set()
        for decision_id in meta.get("decisions", []):
            current = decision_id
            # Follow supersede links. The loop is bounded by the "seen" set.
            while current and current not in seen:
                seen.add(current)
                target = index.get(current)
                if target is None:
                    blocks.append("!! %s is linked but does not exist" % current)
                    break
                replacement = target["meta"].get("superseded_by", "")
                if target["meta"].get("status") == "superseded" and replacement:
                    blocks.append("!! %s is superseded by %s" % (current, replacement))
                    current = replacement
                    continue
                blocks.append(block(target))
                break
        phase = index.get(meta.get("phase", ""))
        if phase is not None:
            blocks.append(block(phase))

    if record["kind"] == "decision":
        replacement = index.get(meta.get("superseded_by", ""))
        if replacement is not None:
            blocks.append("!! %s is superseded by %s" % (record_id, replacement["meta"]["id"]))
            blocks.append(block(replacement))
        users = [r for r in records if record_id in r["meta"].get("decisions", [])]
        if users:
            blocks.append("Used by:\n" + "\n".join("  " + summary_line(r) for r in users))

    if record["kind"] == "phase":
        features = [r for r in records if r["kind"] == "feature" and r["meta"].get("phase") == record_id]
        if features:
            blocks.append("Features:\n" + "\n".join("  " + summary_line(r) for r in features))

    if record["kind"] == "idea":
        target = index.get(meta.get("promoted_to", ""))
        if target is not None:
            blocks.append(block(target))

    if record["kind"] in ("feature", "decision"):
        origins = [r for r in records if r["meta"].get("promoted_to") == record_id]
        if origins:
            blocks.append("From ideas:\n" + "\n".join("  " + summary_line(r) for r in origins))

    print("\n\n".join(blocks))
    return 0


def now_focus(root: Path) -> str:
    """Return the Focus section of NOW.md, or the whole file when it has none."""
    now = root / PROJECT_DIR / NOW_NAME
    if not now.is_file():
        return ""
    text = read_text(now)
    lines = text.splitlines()
    for number, line in enumerate(lines):
        if line.strip().lower().startswith("focus"):
            out = []
            for item in lines[number + 1:]:
                if item.strip() and not item.startswith(" ") and not item.startswith("-") and item.rstrip().endswith(":"):
                    break
                out.append(item)
            return "\n".join(out).strip("\n")
    return text.strip("\n")


NOW_SECTIONS = ("Focus", "Next", "Blockers")
FOCUS_ID_RE = re.compile(r"\b(ADR-\d{3,}|F\d{3,}|P\d{2,}|I\d{3,})\b")


def read_now(root: Path) -> tuple:
    """Return (head, sections) of NOW.md. sections maps a name to its lines."""
    now = root / PROJECT_DIR / NOW_NAME
    text = read_text(now) if now.is_file() else NOW_TEMPLATE
    head = []
    sections = {name: [] for name in NOW_SECTIONS}
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        name = stripped[:-1].strip().capitalize() if stripped.endswith(":") else ""
        if name in sections and not stripped.startswith("-"):
            current = name
            continue
        if current is None:
            head.append(line)
        else:
            sections[current].append(line)
    return head, sections


def write_now(root: Path, head: list, sections: dict) -> None:
    lines = [line for line in head if line.strip()]
    for name in NOW_SECTIONS:
        body = [line for line in sections[name] if line.strip()]
        lines += ["", name + ":"] + body
    atomic_write(root / PROJECT_DIR / NOW_NAME, ("\n".join(lines).strip("\n") + "\n").encode("utf-8"), 0o644)


def line_id(line: str) -> str:
    match = FOCUS_ID_RE.search(line)
    return match.group(1) if match else ""


def set_focus(root: Path, index: dict, record_id: str, words: str) -> str:
    """Make record_id the focus. The old focus moves to the top of Next."""
    head, sections = read_now(root)
    record = get_record(index, record_id)
    if record["meta"].get("status") in CLOSED:
        raise PmmError("%s is %s; focus on an open record" % (record_id, record["meta"]["status"]))
    old = [line for line in sections["Focus"] if line.strip()]
    sections["Next"] = [line for line in sections["Next"] if line_id(line) != record_id]
    for line in reversed(old):
        old_id = line_id(line)
        if old_id and old_id != record_id and old_id in index \
                and index[old_id]["meta"].get("status") not in CLOSED \
                and all(line_id(n) != old_id for n in sections["Next"]):
            sections["Next"].insert(0, line)
    title = record["meta"].get("title", "")
    sections["Focus"] = ["- %s %s%s" % (record_id, title, (". " + words) if words else "")]
    write_now(root, head, sections)
    return "focus: %s %s" % (record_id, title)


def advance_focus(root: Path, index: dict, closed_id: str) -> str:
    """After closed_id is done: drop it from Focus and pull the first Next id up."""
    head, sections = read_now(root)
    before = list(sections["Focus"])
    sections["Focus"] = [line for line in sections["Focus"] if line_id(line) != closed_id]
    sections["Next"] = [line for line in sections["Next"] if line_id(line) != closed_id]
    if sections["Focus"] == before and not any(line_id(n) == closed_id for n in before):
        return ""
    if not [line for line in sections["Focus"] if line.strip()]:
        for line in sections["Next"]:
            next_id = line_id(line)
            if next_id and next_id in index and index[next_id]["meta"].get("status") not in CLOSED:
                sections["Next"].remove(line)
                sections["Focus"] = [line]
                write_now(root, head, sections)
                return "focus is now %s %s" % (next_id, index[next_id]["meta"].get("title", ""))
    write_now(root, head, sections)
    return "focus is empty; set it with: pmm focus ID"


def cmd_focus(args) -> int:
    root = find_root()
    records = load_records(root)
    index = index_by_id(records)
    if not args.id:
        head, sections = read_now(root)
        focus = [line for line in sections["Focus"] if line.strip()]
        print("\n".join(focus) if focus else "focus is empty; set it with: pmm focus ID")
        nxt = [line for line in sections["Next"] if line.strip()]
        if nxt:
            print("Next:\n" + "\n".join(nxt))
        return 0
    record_id = args.id
    if kind_of_id(record_id) is None:
        record_id = resolve_id(records, " ".join([args.id] + args.words))
        words = ""
    else:
        words = " ".join(args.words)
    print(set_focus(root, index, record_id, words))
    return 0


def rank(record: dict) -> int:
    order = {"in_progress": 0, "proposed": 1, "planned": 2, "accepted": 3, "open": 4}
    return order.get(record["meta"].get("status", ""), 9)


def resolve_id(records: list[dict], text: str) -> str:
    """Return the id of the one open record whose title has all words of text."""
    words = [w for w in text.lower().split() if w]
    if not words:
        raise PmmError("give a record id or a title")
    open_records = [r for r in records if r["meta"].get("status") not in CLOSED]
    hits = [r for r in open_records if all(w in r["meta"].get("title", "").lower() for w in words)]
    if not hits:
        hits = [r for r in open_records
                if all(w in (r["meta"].get("title", "") + "\n" + r["body"]).lower() for w in words)]
    if len(hits) == 1:
        return hits[0]["meta"]["id"]
    if not hits:
        raise PmmError("no open record matches '%s'. Try: pmm search %s" % (text, text))
    raise PmmError("'%s' matches %d open records, use the id:\n  %s"
                   % (text, len(hits), "\n  ".join(summary_line(r) for r in hits[:MAX_RELATED])))


def cmd_brief(args) -> int:
    """Print everything needed to start work on one record, in one call.

    For a feature: the feature, its decisions (supersede links followed),
    its phase, the other open records on its files, the focus of NOW.md
    and the files to read. Nothing is printed twice.
    """
    root = find_root()
    records = load_records(root)
    index = index_by_id(records)
    record_id = " ".join(args.id)
    if not record_id:
        record_id = line_id(now_focus(root))
        if not record_id:
            # No focus record. The status is the best one-call answer.
            return cmd_status(args)
    elif kind_of_id(record_id) is None:
        record_id = resolve_id(records, record_id)
    record = get_record(index, record_id)
    meta = record["meta"]
    shown = {record_id}
    blocks = [block(record)]

    for decision_id in meta.get("decisions", []):
        current = decision_id
        while current and current not in shown:
            shown.add(current)
            target = index.get(current)
            if target is None:
                blocks.append("!! %s is linked but does not exist" % current)
                break
            replacement = target["meta"].get("superseded_by", "")
            if target["meta"].get("status") == "superseded" and replacement:
                blocks.append("!! %s is superseded by %s" % (current, replacement))
                current = replacement
                continue
            blocks.append(block(target))
            break

    phase = index.get(meta.get("phase", ""))
    if phase is not None and phase["meta"]["id"] not in shown:
        shown.add(phase["meta"]["id"])
        goal = sections_of(phase["body"]).get("Goal", "")
        blocks.append("Phase: " + summary_line(phase) + ("\n  " + goal.replace("\n", "\n  ") if goal else ""))

    files = [entry.strip("/") for entry in meta.get("files", [])]
    if files:
        related = []
        for other in records:
            other_id = other["meta"].get("id", "")
            if other_id in shown or other["meta"].get("status") in CLOSED:
                continue
            for entry in other["meta"].get("files", []):
                entry = entry.strip("/")
                if any(f == entry or f.startswith(entry + "/") or entry.startswith(f + "/") for f in files):
                    related.append(other)
                    break
        related.sort(key=rank)
        if related:
            lines = ["Other open records on these files (follow their decisions too):"]
            for other in related[:MAX_RELATED]:
                lines.append("  " + summary_line(other))
            if len(related) > MAX_RELATED:
                lines.append("  (%d more: pmm about PATH)" % (len(related) - MAX_RELATED))
            blocks.append("\n".join(lines))
        missing = [f for f in files if not (root / f).exists()]
        blocks.append("Files to read: " + ", ".join(files)
                      + ("\n  (not found: " + ", ".join(missing) + ")" if missing else ""))

    always = parse_list(read_config(root).get("read", ""))
    if always:
        blocks.append("Project files to know: " + ", ".join(always))
    focus = now_focus(root)
    if focus:
        blocks.append("Focus (NOW.md):\n" + focus)
    print("\n\n".join(blocks))
    if args.files:
        for entry in list(dict.fromkeys(files + always)):
            path = root / entry
            targets = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
            for target in targets:
                print("\n--- %s ---" % rel(target, root))
                if not target.is_file():
                    print("(not found)")
                    continue
                data = target.read_bytes()
                if b"\0" in data[:4096]:
                    print("(binary, %d bytes)" % len(data))
                    continue
                if len(data) > MAX_CAT_BYTES:
                    print("(%d bytes, first %d shown)" % (len(data), MAX_CAT_BYTES))
                    data = data[:MAX_CAT_BYTES]
                print(data.decode("utf-8", errors="replace").rstrip("\n"))
    return 0


def run_quiet(command, cwd: Path):
    try:
        return subprocess.run(command, cwd=str(cwd), capture_output=True, text=True, check=False)
    except OSError as exc:
        raise PmmError("cannot run %s: %s" % (command[0], exc)) from None


def cmd_done(args) -> int:
    """Finish a feature in one call: test, acceptance, status, check, commit."""
    root = find_root()
    records = load_records(root)
    index = index_by_id(records)
    record = get_record(index, args.id)
    if record["kind"] != "feature":
        raise PmmError("done applies to features. For a decision: pmm set %s status=accepted" % args.id)
    if record["meta"].get("status") == "done":
        raise PmmError("%s is done already" % args.id)
    if find_git_dir(root) is None:
        raise PmmError("not a Git repository")

    test = args.test or read_config(root).get("test", "")
    if not test and not args.no_test:
        raise PmmError("no test command. Pass --test CMD, set 'test:' in %s/%s, or use --no-test"
                       % (PROJECT_DIR, CONFIG_NAME))
    if test and not args.no_test:
        result = run_quiet(["sh", "-c", test], root)
        if result.returncode != 0:
            print((result.stdout + result.stderr).rstrip("\n"), file=sys.stderr)
            raise PmmError("tests failed (%s). Nothing changed." % test)
        print("tests passed: %s" % test)

    if args.acceptance:
        record["body"] = set_section(record["body"], "Acceptance", read_arg_text(args.acceptance), args.append)
    record["meta"]["status"] = "done"
    counts = {}
    for other in records:
        counts[other["meta"].get("id", "")] = counts.get(other["meta"].get("id", ""), 0) + 1
    errors, warnings = validate(record, index, counts)
    if errors:
        raise PmmError("not done: " + "; ".join(errors))
    write_record(record)
    for warning in warnings:
        print("warning: %s" % warning, file=sys.stderr)
    print("set %s status=done" % args.id)

    check_args = argparse.Namespace(strict=False, hook=False)
    if check_records(check_args) != 0:
        raise PmmError("check failed. The record is changed but not committed.")

    note = advance_focus(root, index, args.id)
    if note:
        print(note)

    title = record["meta"].get("title", "")
    message = "%s: %s" % (args.id, args.message or title)
    if run_quiet(["git", "add", "-A"], root).returncode != 0:
        raise PmmError("git add failed")
    result = run_quiet(["git", "commit", "-q", "-m", message], root)
    if result.returncode != 0:
        raise PmmError("git commit failed: %s" % (result.stdout + result.stderr).strip())
    head = run_quiet(["git", "rev-parse", "--short", "HEAD"], root).stdout.strip()
    print("committed %s %s" % (head, message))
    return 0


def cmd_search(args) -> int:
    root = find_root()
    terms = [term.lower() for term in args.terms]
    paths = [record["path"] for record in load_records(root)]
    now = root / PROJECT_DIR / NOW_NAME
    if now.is_file():
        paths.append(now)
    found = 0
    for path in paths:
        text = read_text(path)
        lowered = text.lower()
        if not all(term in lowered for term in terms):
            continue
        found += 1
        if found > MAX_SEARCH_FILES:
            continue
        hits = []
        for number, line in enumerate(text.splitlines(), start=1):
            line_lowered = line.lower()
            if any(term in line_lowered for term in terms):
                hits.append((number, line.strip()))
        print("%s (%d lines)" % (rel(path, root), len(hits)))
        if not args.files:
            for number, line in hits[:MAX_SEARCH_LINES]:
                print("  %d: %s" % (number, line[:160]))
    if found > MAX_SEARCH_FILES:
        print("(%d more files match; narrow the terms)" % (found - MAX_SEARCH_FILES))
    if found == 0:
        print("no records match", file=sys.stderr)
        return 1
    return 0


def place_text(place: dict, verbose: bool) -> str:
    lines = ["%s -> %s" % (place["name"], place["path"])]
    lines.append("  use:    " + place["use"])
    lines.append("  create: " + place["create"])
    if verbose:
        lines.append("  then:   " + place["then"])
    return "\n".join(lines)


def normalize_word(word: str) -> str:
    word = word.lower().strip("\"'.,;:!?()[]{}")
    if len(word) > 3 and word.endswith("s"):
        word = word[:-1]
    return word


def match_places(text_words) -> list:
    """Return the places that match the words best, or an empty list."""
    words = {normalize_word(w) for arg in text_words for w in arg.split()}
    words.discard("")
    scored = []
    for place in PLACES:
        vocabulary = {normalize_word(w) for w in place["words"]} | {place["name"]}
        score = len(words & vocabulary)
        if score:
            scored.append((score, place))
    if not scored:
        return []
    best = max(score for score, _ in scored)
    return [place for score, place in scored if score == best]


def cmd_where(args) -> int:
    """Print the place for a new piece of knowledge.

    Without words, print the full table. With words, print the best
    match. Several places with the same score are all printed.
    """
    if not any(arg.strip() for arg in args.words):
        print("\n\n".join(place_text(place, True) for place in PLACES))
        return 0
    hits = match_places(args.words)
    if not hits:
        print("no match for: %s. Pick one:" % " ".join(args.words), file=sys.stderr)
        print("\n".join(place_text(place, False) for place in PLACES))
        return 1
    if len(hits) > 1:
        print("ambiguous: %s" % ", ".join(place["name"] for place in hits), file=sys.stderr)
    print("\n\n".join(place_text(place, True) for place in hits))
    return 0


def cmd_add(args) -> int:
    """Create a record from plain words. The kind comes from the words.

    Words that name no kind become an idea, the cheap catch-all.
    """
    root = find_root()
    title = check_title(" ".join(args.words))
    title = title[0].upper() + title[1:]
    if args.kind:
        kind = normalize_kind(args.kind)
    else:
        hits = [place for place in match_places(args.words) if place["name"] in KINDS]
        kind = hits[0]["name"] if len(hits) == 1 else "idea"
    new_args = argparse.Namespace(kind=kind, title=title, status=None, phase=None, tags=None,
                                  decisions=None, files=None, section=None)
    cmd_new(new_args)
    records = load_records(root)
    index = index_by_id(records)
    record = max((r for r in records if r["kind"] == kind), key=lambda r: r["meta"].get("id", ""))
    record_id = record["meta"]["id"]
    if kind == "idea" and not args.kind:
        print("stored as an idea. Another kind: pmm add --kind feature ...")
    if args.now:
        print(set_focus(root, index, record_id, ""))
    elif args.next:
        head, sections = read_now(root)
        sections["Next"].append("- %s %s" % (record_id, title))
        write_now(root, head, sections)
        print("queued under Next")
    return 0


def cmd_board(args) -> int:
    """Print one screen: focus, next, blockers, and counts per phase."""
    root = find_root()
    records = load_records(root)
    head, sections = read_now(root)
    for name in NOW_SECTIONS:
        lines = [line for line in sections[name] if line.strip()]
        print("%s:" % name)
        print("\n".join(lines) if lines else "- none")
    phases = [r for r in records if r["kind"] == "phase"]
    features = [r for r in records if r["kind"] == "feature"]
    if phases:
        print()
        print("%-24s %8s %12s %6s" % ("Phase", "planned", "in_progress", "done"))
        for phase in phases:
            pid = phase["meta"].get("id", "")
            mine = [f for f in features if f["meta"].get("phase") == pid]
            counts = {name: sum(1 for f in mine if f["meta"].get("status") == name)
                      for name in ("planned", "in_progress", "done")}
            label = "%s %s" % (pid, phase["meta"].get("title", ""))
            marker = "*" if phase["meta"].get("status") == "in_progress" else " "
            print("%s%-23s %8d %12d %6d" % (marker, label[:23], counts["planned"], counts["in_progress"], counts["done"]))
    ideas = sum(1 for r in records if r["kind"] == "idea" and r["meta"].get("status") == "open")
    proposed = sum(1 for r in records if r["kind"] == "decision" and r["meta"].get("status") == "proposed")
    print()
    print("Ideas open: %d   Decisions proposed: %d   Records: %d" % (ideas, proposed, len(records)))
    return 0


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:40].strip("-") or "untitled"


def next_id(kind: str, records: list[dict]) -> str:
    spec = KINDS[kind]
    highest = 0
    for record in records:
        if record["kind"] != kind:
            continue
        # Use the file name too, so a record with a broken frontmatter
        # block still reserves its number.
        for candidate in (record["meta"].get("id", ""), record["path"].stem):
            match = re.match(r"^%s(\d+)" % re.escape(spec["prefix"]), candidate)
            if match:
                highest = max(highest, int(match.group(1)))
    return "%s%0*d" % (spec["prefix"], spec["width"], highest + 1)


def check_title(title: str) -> str:
    title = title.strip()
    if not title or "\n" in title or "\r" in title:
        raise PmmError("title must be one line and not empty")
    return title


def cmd_new(args) -> int:
    root = find_root()
    kind = normalize_kind(args.kind)
    spec = KINDS[kind]
    records = load_records(root)
    index = index_by_id(records)
    title = check_title(args.title)

    meta = {"id": next_id(kind, records), "title": title, "status": spec["status"][0]}
    if kind == "feature":
        meta["phase"] = args.phase or ""
        meta["tags"] = parse_list(args.tags or "")
        meta["decisions"] = parse_list(args.decisions or "")
        meta["files"] = parse_list(args.files or "")
    elif kind == "decision":
        meta["date"] = date.today().isoformat()
        meta["tags"] = parse_list(args.tags or "")
        meta["superseded_by"] = ""
        meta["files"] = parse_list(args.files or "")
    if kind in ("phase", "idea") and args.files:
        raise PmmError("--files applies to features and decisions only")
    elif kind == "idea":
        meta["tags"] = parse_list(args.tags or "")
        meta["promoted_to"] = ""
    if kind != "feature" and (args.phase or args.decisions):
        raise PmmError("--phase and --decisions apply to features only")
    if kind == "phase" and args.tags:
        raise PmmError("--tags does not apply to phases")
    if args.status:
        meta["status"] = args.status

    directory = root / PROJECT_DIR / spec["dir"]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("%s-%s.md" % (meta["id"], slugify(title)))
    if path.exists():
        raise PmmError("%s exists already" % rel(path, root))
    body = "\n\n".join("## " + section for section in spec["sections"])
    for pair in args.section or []:
        name, equals, text = pair.partition("=")
        if not equals or not name.strip():
            raise PmmError("expected --section Name=text, got: %s" % pair)
        body = set_section(body, name.strip(), read_arg_text(text), False)
    record = {"path": path, "kind": kind, "meta": meta, "body": body, "problems": [], "root": root}

    index[meta["id"]] = record
    errors, _ = validate(record, index, {meta["id"]: 1})
    if errors:
        raise PmmError("not created: " + "; ".join(errors))
    write_record(record)
    print("created %s" % rel(path, root))
    return 0


def read_arg_text(text: str) -> str:
    """Return text, or stdin when text is "-"."""
    if text == "-":
        return sys.stdin.read()
    return text


def set_section(body: str, name: str, text: str, append: bool) -> str:
    """Replace or extend the text under "## name". The heading must exist."""
    lines = body.splitlines()
    start = None
    for number, line in enumerate(lines):
        if line.startswith("## ") and line[3:].strip() == name:
            start = number
            break
    if start is None:
        present = [line[3:].strip() for line in lines if line.startswith("## ")]
        raise PmmError("no section '%s' (sections: %s)" % (name, ", ".join(present)))
    end = len(lines)
    for number in range(start + 1, len(lines)):
        if lines[number].startswith("## "):
            end = number
            break
    old = "\n".join(lines[start + 1:end]).strip("\n")
    new = text.strip("\n")
    if append and old:
        new = old + "\n" + new
    content = [new] if new else []
    return "\n".join(lines[:start + 1] + content + [""] + lines[end:]).strip("\n")


def cmd_write(args) -> int:
    root = find_root()
    records = load_records(root)
    index = index_by_id(records)
    record = get_record(index, args.id)
    if record["problems"]:
        raise PmmError("%s has a broken frontmatter block. Repair it first."
                       % rel(record["path"], root))
    record["body"] = set_section(record["body"], args.section, read_arg_text(args.text), args.append)
    write_record(record)
    print("wrote %s of %s" % (args.section, args.id))
    return 0


def cmd_about(args) -> int:
    """Print the records whose files entries cover the given paths."""
    root = find_root()
    records = load_records(root)
    wanted = []
    for raw in args.paths:
        path = Path(raw)
        if path.is_absolute():
            try:
                path = path.resolve().relative_to(root)
            except ValueError:
                raise PmmError("%s is outside the repository" % raw) from None
        wanted.append(str(path).strip("/"))
    hits = []
    for record in records:
        if record["meta"].get("status") in CLOSED and not args.all:
            continue
        for entry in record["meta"].get("files", []):
            entry = entry.strip("/")
            if any(w == entry or w.startswith(entry + "/") for w in wanted):
                hits.append(record)
                break
    if not hits:
        print("no open records name %s" % ", ".join(wanted), file=sys.stderr)
        return 1
    for record in hits:
        print(summary_line(record))
    return 0


def cmd_set(args) -> int:
    root = find_root()
    records = load_records(root)
    index = index_by_id(records)
    record = get_record(index, args.id)
    if record["problems"]:
        raise PmmError("%s has a broken frontmatter block. Repair it first: %s"
                      % (rel(record["path"], root), "; ".join(record["problems"])))
    spec = KINDS[record["kind"]]
    for pair in args.pairs:
        key, equals, value = pair.partition("=")
        key = key.strip()
        if not equals or not key:
            raise PmmError("expected key=value, got: %s" % pair)
        if key == "id":
            raise PmmError("id cannot be changed")
        if key not in spec["keys"]:
            raise PmmError("key '%s' is not valid for a %s (valid: %s)"
                          % (key, record["kind"], ", ".join(spec["keys"][1:])))
        if key in LIST_KEYS:
            record["meta"][key] = parse_list(value)
        elif key == "title":
            record["meta"][key] = check_title(value)
        else:
            record["meta"][key] = value.strip()

    counts = {}
    for other in records:
        other_id = other["meta"].get("id", "")
        counts[other_id] = counts.get(other_id, 0) + 1
    errors, warnings = validate(record, index, counts)
    if errors:
        raise PmmError("not changed: " + "; ".join(errors))
    write_record(record)
    print("updated %s" % rel(record["path"], root))
    for warning in warnings:
        print("warning: %s" % warning, file=sys.stderr)
    return 0


def cmd_check(args) -> int:
    if args.hook:
        # Stop hook of Claude Code. The hook input is JSON on stdin.
        # stop_hook_active is true when this hook already blocked once.
        try:
            hook_input = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            hook_input = {}
        if isinstance(hook_input, dict) and hook_input.get("stop_hook_active"):
            return 0
        sys.stdout = sys.stderr
        code = check_records(args)
        return 2 if code else 0
    return check_records(args)


def check_records(args) -> int:
    root = find_root()
    records = load_records(root)
    index = index_by_id(records)
    counts = {}
    for record in records:
        record_id = record["meta"].get("id", "")
        counts[record_id] = counts.get(record_id, 0) + 1
    error_count = 0
    warning_count = 0
    for record in records:
        errors, warnings = validate(record, index, counts)
        for message in errors:
            print("%s: error: %s" % (rel(record["path"], root), message))
        for message in warnings:
            print("%s: warning: %s" % (rel(record["path"], root), message))
        error_count += len(errors)
        warning_count += len(warnings)
    now = root / PROJECT_DIR / NOW_NAME
    if now.is_file():
        now_lines = len(read_text(now).splitlines())
        if now_lines > MAX_NOW_LINES:
            print("%s/%s: warning: %d lines, keep it under %d"
                  % (PROJECT_DIR, NOW_NAME, now_lines, MAX_NOW_LINES))
            warning_count += 1
    print("%d records, %d errors, %d warnings" % (len(records), error_count, warning_count))
    if args.strict and warning_count:
        return 1
    return 1 if error_count else 0


def cmd_log(args) -> int:
    """Print the commits whose message names the record id."""
    root = find_root()
    get_record(index_by_id(load_records(root)), args.id)
    if find_git_dir(root) is None:
        raise PmmError("not a Git repository")
    command = ["git", "-C", str(root), "log", "--oneline", "--date=short", "--format=%h %ad %s",
               "-n", str(args.limit), "--fixed-strings", "--grep", args.id]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise PmmError("cannot run git: %s" % exc) from None
    if result.returncode != 0:
        raise PmmError("git log failed: %s" % result.stderr.strip())
    output = result.stdout.strip("\n")
    if not output:
        print("no commits name %s" % args.id, file=sys.stderr)
        return 1
    print(output)
    return 0


def cmd_instruct(args) -> int:
    if args.hooks:
        print(json.dumps(HOOKS_SNIPPET, indent=2))
        return 0
    if args.skill:
        root = find_root()
        path = root / SKILL_PATH
        if path.is_file() and read_text(path) == SKILL_TEXT:
            print("unchanged %s" % SKILL_PATH)
            return 0
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, SKILL_TEXT.encode("utf-8"), 0o644)
        print("wrote %s. Use it as: /pmm ID, /pmm title words, /pmm new WORDS" % SKILL_PATH)
        return 0
    print(AGENTS_SNIPPET, end="")
    return 0


# --------------------------------------------------------------------------
# Self-update
# --------------------------------------------------------------------------


def update_configured() -> bool:
    return bool(os.environ.get("PMM_UPDATE_URL")) or not UPDATE_REPO.startswith("OWNER/")


def update_base() -> str:
    """Return the URL prefix for the release files.

    PMM_UPDATE_URL replaces the GitHub address, for example with an
    internal mirror. Plain HTTP is accepted for the local host only.
    """
    base = os.environ.get("PMM_UPDATE_URL", "").rstrip("/")
    if not base:
        if not update_configured():
            raise PmmError("self-update is not configured. Set UPDATE_REPO in pmm.")
        base = "https://github.com/%s/releases/latest/download" % UPDATE_REPO
    local = base.startswith("http://127.0.0.1") or base.startswith("http://localhost")
    if not base.startswith("https://") and not local:
        raise PmmError("update URL must use https: %s" % base)
    return base


def fetch(url: str, timeout: int, limit: int) -> bytes:
    """Download url. Refuse a body that is larger than limit bytes."""
    request = urllib.request.Request(url, headers={"User-Agent": "pmm/" + __version__})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise PmmError("download is larger than %d bytes: %s" % (limit, url))
    return data


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = VERSION_RE.search(text)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def self_update(timeout: int, check_only: bool) -> str:
    """Replace this script with the latest release if that release is newer.

    The checksum file comes from the same release as the script. It
    detects a damaged download. It does not prove who published the
    release. Review the change to .project/pmm before you commit it.
    """
    base = update_base()
    current = parse_version('__version__ = "%s"' % __version__)
    if current is None:
        raise PmmError("cannot read own version")

    words = fetch(base + "/" + ASSET_NAME + ".sha256", timeout, 1024).decode("ascii", "replace").split()
    wanted = words[0].lower() if words else ""
    if not SHA256_RE.match(wanted):
        raise PmmError("checksum file is not valid")

    own_path = Path(__file__).resolve()
    if hashlib.sha256(own_path.read_bytes()).hexdigest() == wanted:
        return "up to date (%s)" % __version__

    data = fetch(base + "/" + ASSET_NAME, timeout, MAX_SCRIPT_BYTES)
    if hashlib.sha256(data).hexdigest() != wanted:
        raise PmmError("checksum of the download does not match")
    if not data.startswith(b"#!"):
        raise PmmError("download is not a script")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise PmmError("download is not UTF-8 text") from None
    latest = parse_version(text)
    if latest is None:
        raise PmmError("download has no version")
    if latest <= current:
        return "no newer release (local %s, release %d.%d.%d)" % ((__version__,) + latest)
    # A release that this interpreter cannot compile is refused.
    try:
        compile(text, ASSET_NAME, "exec")
    except (SyntaxError, ValueError) as exc:
        raise PmmError("this Python cannot compile the release: %s" % exc) from None

    label = "%s -> %d.%d.%d" % ((__version__,) + latest)
    if check_only:
        return "update available: " + label
    atomic_write(own_path, data, 0o755)
    return "updated " + label


def auto_update(root: Path) -> None:
    """Run self_update at most one time in each interval. Never raise."""
    try:
        if os.environ.get("PMM_NO_UPDATE") or os.environ.get("CI"):
            return
        if not update_configured():
            return
        if Path(__file__).resolve().parent != (root / PROJECT_DIR).resolve():
            return
        git_dir = find_git_dir(root)
        if git_dir is None:
            return
        stamp = git_dir / STAMP_NAME
        now = time.time()
        try:
            last = stamp.stat().st_mtime
        except OSError:
            last = 0.0
        if 0 <= now - last < UPDATE_INTERVAL_S:
            return
        # Write the stamp first. A failed check is not repeated until the
        # next interval.
        stamp.write_text("%d\n" % int(now), encoding="ascii")
        message = self_update(AUTO_TIMEOUT_S, False)
        if message.startswith("updated"):
            print("pmm: %s. Review and commit %s/%s." % (message, PROJECT_DIR, SCRIPT_NAME),
                  file=sys.stderr)
    except Exception as exc:  # The update must not break the command.
        if os.environ.get("PMM_DEBUG"):
            print("pmm: update check failed: %s" % exc, file=sys.stderr)


def cmd_self_update(args) -> int:
    print(self_update(MANUAL_TIMEOUT_S, args.check))
    return 0


def cmd_version(args) -> int:
    print(__version__)
    return 0


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pmm", description="Project memory manager.")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands.required = False

    sub = commands.add_parser("init", help="create .project/ and copy pmm into it")
    sub.add_argument("--force", action="store_true", help="replace an existing .project/pmm")
    sub.add_argument("--bare", action="store_true", help="records only; no CLAUDE.md, skill or hooks")
    sub.set_defaults(func=cmd_init)

    sub = commands.add_parser("add", help="create a record from plain words; the kind is detected")
    sub.add_argument("words", nargs="+")
    sub.add_argument("--kind", help="feature, decision, phase or idea")
    sub.add_argument("--now", action="store_true", help="make it the focus")
    sub.add_argument("--next", action="store_true", help="queue it under Next")
    sub.set_defaults(func=cmd_add)

    sub = commands.add_parser("board", help="one screen: focus, next, blockers, counts (default)")
    sub.set_defaults(func=cmd_board)

    sub = commands.add_parser("status", help="show work in progress and NOW.md")
    sub.set_defaults(func=cmd_status)

    sub = commands.add_parser("list", help="list records, one line each")
    sub.add_argument("kind", nargs="?", help="feature, decision, phase or idea")
    sub.add_argument("--status")
    sub.add_argument("--tag")
    sub.add_argument("--phase")
    sub.add_argument("--all", action="store_true", help="include done, dropped, rejected, superseded, promoted")
    sub.set_defaults(func=cmd_list)

    sub = commands.add_parser("show", help="print one record")
    sub.add_argument("id")
    sub.set_defaults(func=cmd_show)

    sub = commands.add_parser("brief", help="everything needed to start work on a record, one call")
    sub.add_argument("id", nargs="*", help="an id, or words of the title; default: the focus of NOW.md")
    sub.add_argument("--files", action="store_true", help="also print the contents of the files to read")
    sub.set_defaults(func=cmd_brief)

    sub = commands.add_parser("focus", help="show or set the focus in NOW.md; done advances it")
    sub.add_argument("id", nargs="?", help="an id or title words; the old focus moves to Next")
    sub.add_argument("words", nargs="*", help="a short note after the title")
    sub.set_defaults(func=cmd_focus)

    sub = commands.add_parser("done", help="finish a feature: test, acceptance, status, check, commit")
    sub.add_argument("id")
    sub.add_argument("acceptance", nargs="?", help="text for the Acceptance section; '-' reads stdin")
    sub.add_argument("--append", action="store_true", help="add to Acceptance instead of replacing it")
    sub.add_argument("--test", help="test command; default: 'test:' in .project/config")
    sub.add_argument("--no-test", action="store_true")
    sub.add_argument("-m", "--message", help="commit subject after the id; default: the title")
    sub.set_defaults(func=cmd_done)

    sub = commands.add_parser("context", help="print a record and the records it links to")
    sub.add_argument("id")
    sub.set_defaults(func=cmd_context)

    sub = commands.add_parser("search", help="find records that contain all terms")
    sub.add_argument("terms", nargs="+")
    sub.add_argument("-l", "--files", action="store_true", help="print file names only")
    sub.set_defaults(func=cmd_search)

    sub = commands.add_parser("where", help="say where a new idea, feature, decision, phase or note goes")
    sub.add_argument("words", nargs="*", help="what you want to record, in a few words")
    sub.set_defaults(func=cmd_where)

    sub = commands.add_parser("new", help="create a record from a template")
    sub.add_argument("kind", help="feature, decision, phase or idea")
    sub.add_argument("title")
    sub.add_argument("--status")
    sub.add_argument("--phase", help="feature only, for example P02")
    sub.add_argument("--tags", help="comma-separated")
    sub.add_argument("--decisions", help="feature only, comma-separated ADR ids")
    sub.add_argument("--files", help="feature or decision, comma-separated paths it governs")
    sub.add_argument("-s", "--section", action="append", metavar="Name=text",
                     help="fill a body section; repeatable; text '-' reads stdin")
    sub.set_defaults(func=cmd_new)

    sub = commands.add_parser("set", help="change frontmatter fields of a record")
    sub.add_argument("id")
    sub.add_argument("pairs", nargs="+", metavar="key=value")
    sub.set_defaults(func=cmd_set)

    sub = commands.add_parser("check", help="validate all records (exit 1 on errors)")
    sub.add_argument("--strict", action="store_true", help="warnings also give exit 1")
    sub.add_argument("--hook", action="store_true",
                     help="run as a Claude Code Stop hook: report on stderr, exit 2 on errors")
    sub.set_defaults(func=cmd_check)

    sub = commands.add_parser("write", help="replace the text of a body section")
    sub.add_argument("id")
    sub.add_argument("section", help="for example Goal or Decision")
    sub.add_argument("text", help="the new text; '-' reads stdin")
    sub.add_argument("--append", action="store_true", help="add to the section instead")
    sub.set_defaults(func=cmd_write)

    sub = commands.add_parser("about", help="records whose files cover the given paths")
    sub.add_argument("paths", nargs="+")
    sub.add_argument("--all", action="store_true", help="include closed records")
    sub.set_defaults(func=cmd_about)

    sub = commands.add_parser("log", help="print the commits whose message names a record id")
    sub.add_argument("id")
    sub.add_argument("-n", "--limit", type=int, default=MAX_LOG_LINES)
    sub.set_defaults(func=cmd_log)

    sub = commands.add_parser("instruct", help="print the block for AGENTS.md or CLAUDE.md")
    sub.add_argument("--hooks", action="store_true",
                     help="print Claude Code hooks for .claude/settings.json instead")
    sub.add_argument("--skill", action="store_true",
                     help="write the Claude Code skill to %s instead" % SKILL_PATH)
    sub.set_defaults(func=cmd_instruct)

    sub = commands.add_parser("self-update", help="install the latest release of pmm")
    sub.add_argument("--check", action="store_true", help="report only, change nothing")
    sub.set_defaults(func=cmd_self_update)

    sub = commands.add_parser("version", help="print the version")
    sub.set_defaults(func=cmd_version)
    return parser


def main(argv: list[str]) -> int:
    if argv[:1] == ["--instruct"]:
        argv = ["instruct"] + argv[1:]
    args = build_parser().parse_args(argv)
    if args.command is None:
        args.func = cmd_board
    try:
        code = args.func(args)
    except PmmError as exc:
        print("pmm: %s" % exc, file=sys.stderr)
        return 2
    except OSError as exc:
        print("pmm: %s" % exc, file=sys.stderr)
        return 2
    if args.command not in ("init", "self-update", "version", "instruct"):
        try:
            auto_update(find_root())
        except PmmError:
            pass
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
