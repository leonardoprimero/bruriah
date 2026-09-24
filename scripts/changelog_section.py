#!/usr/bin/env python3
"""Print one CHANGELOG.md section's body, for the release workflow's GitHub Release notes.

`python scripts/changelog_section.py <version>` prints the body of that version's `## [<version>]`
section -- the heading line itself excluded -- to stdout and exits 0. An unknown version prints a
message to stderr and exits 1 instead of printing nothing, so a workflow step piping this into
`gh release create --notes-file` fails loudly rather than publishing an empty release body.
"`Unreleased`" is accepted like any other version, since `## [Unreleased]` is a section heading
like any other.

Stdlib only, on purpose: this runs inside the release workflow after `dist/` has already been
built, and pulling in a real Markdown parser for one regex's worth of work would be one more
dependency the supply-chain comments in `.github/workflows/release.yml` would have to account for.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHANGELOG = REPOSITORY_ROOT / "CHANGELOG.md"

# Matches "## [1.3.1] — 2026-09-20" or "## [Unreleased]"; the version is whatever sits between the
# brackets, and anything after the closing bracket on the same line (a date, an em dash) is not
# part of it.
_SECTION_HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\][^\n]*$", re.MULTILINE)


def section_body(changelog_text: str, version: str) -> str | None:
    """Return the body of the `## [<version>]` section, or `None` if no such section exists.

    The body runs from just after the matched heading line up to the next `## [...]` heading (or
    end of file), with the heading itself excluded and leading/trailing blank lines trimmed.
    """
    headings = list(_SECTION_HEADING.finditer(changelog_text))
    for index, match in enumerate(headings):
        if match.group("version") == version:
            start = match.end()
            end = headings[index + 1].start() if index + 1 < len(headings) else len(changelog_text)
            return changelog_text[start:end].strip("\n")
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("version", help='the version to extract, e.g. "1.3.1" or "Unreleased"')
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_CHANGELOG,
        help=f"path to CHANGELOG.md (default: {DEFAULT_CHANGELOG})",
    )
    args = parser.parse_args(argv)

    text = args.path.read_text(encoding="utf-8")
    body = section_body(text, args.version)
    if body is None:
        print(
            f"changelog_section: no '## [{args.version}]' section found in {args.path}",
            file=sys.stderr,
        )
        return 1
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
