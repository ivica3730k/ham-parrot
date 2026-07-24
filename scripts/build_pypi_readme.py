"""Generate ``readme-pypi.md`` from ``README.md`` with relative links
and images rewritten to absolute GitHub permalinks tied to a release tag.

Usage:
    python scripts/build_pypi_readme.py <tag>          # e.g. v1.0.0

PyPI freezes the README at build time and resolves relative links
against ``pypi.org``, so ``[LICENSE](./LICENSE)`` and ``![](image.png)``
404. Rewriting to ``https://github.com/<owner>/<repo>/{blob,raw}/<tag>/<path>``
keeps them valid forever (permalink at that tag).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = "ivica3730k/ham-parrot"
SRC = Path("README.md")
DST = Path("readme-pypi.md")

# Match a markdown link `[text](target)` and image `![alt](target)`.
# Group 1 = full leading (`[text](` or `![alt](`), group 2 = target,
# group 3 = trailing `)`.
_LINK = re.compile(r"(!?\[[^\]]*\]\()([^)]+)(\))")


def _is_external(target: str) -> bool:
    return (
        target.startswith(("http://", "https://", "mailto:", "#"))
        or target.startswith("//")
    )


def _rewrite(match: re.Match[str], tag: str) -> str:
    lead, target, trail = match.group(1), match.group(2), match.group(3)
    if _is_external(target):
        return match.group(0)
    # Image (starts with `!`) -> raw content endpoint; document -> blob.
    is_image = lead.startswith("!")
    kind = "raw" if is_image else "blob"
    path = target.lstrip("./")
    return f"{lead}https://github.com/{REPO}/{kind}/{tag}/{path}{trail}"


def build(tag: str) -> None:
    if not SRC.exists():
        sys.exit(f"error: {SRC} not found")
    text = SRC.read_text()
    rewritten = _LINK.sub(lambda m: _rewrite(m, tag), text)
    header = (
        f"<!-- generated from README.md at tag {tag} by "
        f"scripts/build_pypi_readme.py; do not hand-edit -->\n\n"
    )
    DST.write_text(header + rewritten)
    print(f"wrote {DST} (tag={tag})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="Release tag (e.g. v1.0.0)")
    args = parser.parse_args()
    build(args.tag)


if __name__ == "__main__":
    main()
