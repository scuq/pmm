# pmm - project memory manager

pmm keeps the project knowledge that an AI coding agent needs in `.project/`.
It stores features, decisions, phases, and ideas as Markdown records.
It answers the agent in one call, decides where a new record goes, and checks every change.

## Usage

    python3 .project/pmm COMMAND [ARGS]

    pmm brief [ID|TITLE WORDS] [--files]   everything for one task, one call
    pmm done ID [ACCEPTANCE]               test, close, check, commit, and advance the focus
    pmm focus [ID]                         show or set the planned task in NOW.md
    pmm where WORDS                        where a new idea, feature, decision, or note goes
    pmm status                             work in progress and NOW.md
    pmm new KIND "Title" [-s Name=text]    create a record from a template
    pmm set ID key=value                   change a frontmatter field
    pmm write ID Section "text"            replace the text of a body section
    pmm check [--strict]                   check all records
    pmm instruct [--hooks|--skill]         the block for CLAUDE.md, the hooks, or the skill

Run `python3 .project/pmm --help` for the full list.

## Download

Latest release, the version that self-update installs:

    https://github.com/scuq/pmm/releases/latest/download/pmm.py

Newest commit on main, before it is released:

    https://raw.githubusercontent.com/scuq/pmm/main/pmm.py

One line to put either one into a project:

    curl -fsSLO https://github.com/scuq/pmm/releases/latest/download/pmm.py && python3 pmm.py init && rm pmm.py

## Description

A long project collects decisions that an agent must follow and features that it must finish.
Without a structure, the agent searches the documentation on every task, misses a decision that a newer one replaced, or edits the wrong file.
pmm gives that knowledge one place, one format, and one command to read it.

Each record is a Markdown file with a flat frontmatter block.
The frontmatter is the index.
There is no separate index file, so the records cannot go out of sync with it.

The agent starts a task with `pmm brief`.
The output holds the record, the decisions it links to, the phase, the related open records, the current focus, and the files to read.
With `--files`, the contents of those files follow in the same output.
The agent then makes all edits in one call and finishes with `pmm done`.
That command runs the tests, writes the acceptance text, sets the status to done, checks all records, and commits as `ID: title`.

`.project/NOW.md` holds the queue.
The first record id under `Focus:` is the planned task.
`pmm brief` with no id takes it, and the Claude Code hook puts it into the context at session start.
`pmm focus ID` makes another record the focus and moves the old one to the top of `Next:`.
When `pmm done` closes the focus, it pulls the first id under `Next:` up, so the queue advances without an edit.

pmm is one Python file.
It uses only the standard library and runs on Python 3.9 or later.
You commit a copy of it in each project, so it works offline and every update is a normal diff.

## Installation

Run this in the root of the repository:

    curl -fsSLO https://github.com/scuq/pmm/releases/latest/download/pmm.py && python3 pmm.py init && rm pmm.py

The command downloads the latest release, creates `.project/`, and copies the script to `.project/pmm`.
The downloaded file is not necessary after that.

To check the download before you run it, use the checksum from the same release:

    curl -fsSLO https://github.com/scuq/pmm/releases/latest/download/pmm.py
    curl -fsSL https://github.com/scuq/pmm/releases/latest/download/pmm.py.sha256 | sha256sum -c
    python3 pmm.py init && rm pmm.py

On macOS, replace `sha256sum -c` with `shasum -a 256 -c`.

Then connect the agent:

1. Add the instruction block to the file your agent reads.
   For Claude Code:

       python3 .project/pmm instruct >> CLAUDE.md

   For an agent that reads AGENTS.md:

       python3 .project/pmm instruct >> AGENTS.md

2. Optional, Claude Code only: install the hooks.
   They put the brief into the context at session start and run `check` before the agent stops.

       mkdir -p .claude && python3 .project/pmm instruct --hooks > .claude/settings.json

   If `.claude/settings.json` exists, copy the `hooks` object into it by hand.

3. Optional, Claude Code only: install the `/pmm` command.
   `/pmm F012` implements a record, `/pmm new WORDS` records new knowledge, and `/pmm` alone takes the focus.
   The skill is user-invoked only, so it costs no context in a session that does not use it.

       python3 .project/pmm instruct --skill

4. Set the test command in `.project/config`, so that `pmm done` can run it:

       test: python3 -m unittest -q

5. Write the first task into `.project/NOW.md` under `Focus:`, with its record id first, or run `pmm focus ID`.

6. Commit `.project/`, the instruction file, and `.claude/` if you created it.

## Configuration

`.project/config` holds one `key: value` per line.

- `test:` The command that `pmm done` runs before it commits. When it is empty, `done` needs `--test CMD` or `--no-test`.
- `read:` Files that every task must know, comma-separated. `brief` names them and `brief --files` prints them. `init` fills the line with README.md and a style guide when they exist.

## Exit status

- 0: Success.
- 1: `check` found an error, or `search`, `about`, `where`, and `log` found no match.
- 2: Invalid input, a refused change, or a failed test in `done`. The message on stderr says why.

## Environment

- `PMM_NO_UPDATE`: When set, the daily update check does not run. `CI` has the same effect.
- `PMM_UPDATE_URL`: The address to download releases from, instead of GitHub. It must use HTTPS.
- `PMM_DEBUG`: When set, a failed update check prints its cause on stderr.

## Files

- `.project/NOW.md`: The sections `Focus:`, `Next:`, and `Blockers:`, one list item per line. Keep it under 40 lines.
- `.claude/skills/pmm/SKILL.md`: The `/pmm` command, written by `instruct --skill`.
- `.project/config`: The settings described above.
- `.project/features/F001-slug.md`, `.project/decisions/ADR-001-slug.md`, `.project/phases/P01-slug.md`, `.project/ideas/I001-slug.md`: The records.
- `.project/pmm`: The committed copy of the script.
- `.git/pmm-last-check`: The time of the last update check, one per clone. It is not committed.

## Updates

Once in 24 hours, a command checks for a newer tagged release.
It replaces `.project/pmm` only when the version is newer, the checksum matches, and the file compiles.
The change stays uncommitted, so you review it in your next diff.
When the network is not available, the check stops without a message.
`pmm self-update` runs the same check at once, and `--check` reports without a change.

The checksum comes from the same release as the script.
It detects a damaged download.
It does not prove who published the release.
The review of the diff is the real safeguard.

## See also

`python3 .project/pmm where` prints the full table of record kinds and their use.
