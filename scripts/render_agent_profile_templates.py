#!/usr/bin/env python3
"""Compose base-harness-template + role fragments into final agent-store profiles.

Single source of truth for CAO-environment operating knowledge that is common to
every agent profile (parent/child/sibling coordination, which "send message" tool
is yours, Handoff vs. Assign, security constraints, memory usage) is
``src/cli_agent_orchestrator/agent_store_templates/base-harness-template.md``. Role
profiles that want to inherit it are stored as thin **fragments** in that same
``agent_store_templates/`` directory: frontmatter ``extends: base-harness-template``
plus ONLY their role-specific body content -- no restatement of the shared material.

``src/cli_agent_orchestrator/agent_store/`` (the directory CAO's own
``_read_agent_profile_source`` actually reads at runtime) has no notion of
``extends`` -- it reads whatever ``.md`` file is there as a complete, standalone
profile. So this script is the composition step: for every fragment with an
``extends`` key, it merges the fragment's frontmatter over the base's (fragment
wins on key collisions; ``mcpServers`` is replaced wholesale, not deep-merged --
no profile needs partial per-server overrides today) and concatenates the base's
body followed by the fragment's body, then writes the fully-resolved result to
``agent_store/<name>.md``. ``extends``/``name``/``description`` are never left as
stray keys from the base; the fragment's own values (or their absence) win.

Usage::

    python scripts/render_agent_profile_templates.py           # regenerate agent_store/*.md
    python scripts/render_agent_profile_templates.py --check   # CI guard: exit 1 on drift

Profiles with no ``extends`` key are left alone -- this script only touches
profiles that are actually composed from a base template.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List

import frontmatter

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = REPO_ROOT / "src" / "cli_agent_orchestrator" / "agent_store_templates"
AGENT_STORE_DIR = REPO_ROOT / "src" / "cli_agent_orchestrator" / "agent_store"


def compose_profile(base_text: str, fragment_text: str) -> str:
    """Merge a base template and a role fragment into one resolved profile.

    Frontmatter: shallow-merged, fragment wins on key collisions (so a fragment's
    own ``name``/``description``/``role`` replace the base's placeholder values).
    ``extends`` is dropped from the result -- it is a composition directive, not a
    runtime profile field. Body: base content first, then the fragment's own
    content, separated by a blank line.
    """
    base = frontmatter.loads(base_text)
    fragment = frontmatter.loads(fragment_text)

    merged_meta = {**base.metadata, **fragment.metadata}
    merged_meta.pop("extends", None)

    merged_body = f"{base.content.strip()}\n\n{fragment.content.strip()}\n"

    post = frontmatter.Post(merged_body, **merged_meta)
    return frontmatter.dumps(post)


def _iter_fragments() -> List[Path]:
    return sorted(
        p
        for p in TEMPLATES_DIR.glob("*.md")
        if frontmatter.loads(p.read_text(encoding="utf-8")).metadata.get("extends")
    )


def _render_all() -> dict[str, str]:
    """Return {profile_name: rendered_text} for every fragment that extends a base."""
    rendered = {}
    for fragment_path in _iter_fragments():
        fragment_text = fragment_path.read_text(encoding="utf-8")
        base_name = frontmatter.loads(fragment_text).metadata["extends"]
        base_path = TEMPLATES_DIR / f"{base_name}.md"
        if not base_path.is_file():
            print(f"ERROR: {fragment_path.name} extends missing base '{base_name}'")
            sys.exit(1)
        base_text = base_path.read_text(encoding="utf-8")
        profile_name = frontmatter.loads(fragment_text).metadata.get("name", fragment_path.stem)
        rendered[profile_name] = compose_profile(base_text, fragment_text)
    return rendered


def check() -> int:
    rendered = _render_all()
    drifted = []
    for name, text in rendered.items():
        dest = AGENT_STORE_DIR / f"{name}.md"
        if not dest.is_file() or dest.read_text(encoding="utf-8") != text:
            drifted.append(name)

    if drifted:
        print("Agent profile template drift detected for: " + ", ".join(sorted(drifted)))
        print("Run `python scripts/render_agent_profile_templates.py` to regenerate.")
        return 1

    print(f"OK: {len(rendered)} templated agent profiles are in sync.")
    return 0


def render() -> int:
    rendered = _render_all()
    AGENT_STORE_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in rendered.items():
        (AGENT_STORE_DIR / f"{name}.md").write_text(text, encoding="utf-8")
    print(f"Rendered {len(rendered)} agent profiles into {AGENT_STORE_DIR}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if any agent_store profile has drifted from its templates (CI guard).",
    )
    args = parser.parse_args()
    return check() if args.check else render()


if __name__ == "__main__":
    sys.exit(main())
