"""Guard tests for the base-harness-template composition mechanism.

Canonical fragments live in ``src/cli_agent_orchestrator/agent_store_templates/``:
one base (``base-harness-template.md``) plus role fragments that extend it via an
``extends`` frontmatter key. ``scripts/render_agent_profile_templates.py`` composes
each fragment with its base into the fully-resolved profile CAO actually reads from
``src/cli_agent_orchestrator/agent_store/``. These tests lock the composition
semantics themselves, and guard against the two files drifting apart silently (the
generated copy must always be regenerated, never hand-edited).
"""

import importlib.util
from pathlib import Path

import pytest

from cli_agent_orchestrator.utils.agent_profiles import parse_agent_profile_text

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = REPO_ROOT / "src" / "cli_agent_orchestrator" / "agent_store_templates"
AGENT_STORE_DIR = REPO_ROOT / "src" / "cli_agent_orchestrator" / "agent_store"
RENDER_COMMAND = "python scripts/render_agent_profile_templates.py"


def _load_render_module():
    spec = importlib.util.spec_from_file_location(
        "_render_agent_profile_templates", REPO_ROOT / "scripts" / "render_agent_profile_templates.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


render_mod = _load_render_module()


class TestComposeProfile:
    """Unit-level checks on the merge semantics."""

    BASE = (
        "---\n"
        "name: base-harness-template\n"
        "description: base\n"
        "mcpServers:\n"
        "  cao-mcp-server:\n"
        "    type: stdio\n"
        "    command: cao-mcp-server\n"
        "    args: []\n"
        "---\n"
        "BASE BODY\n"
    )

    def test_fragment_name_and_description_win_over_base(self):
        fragment = (
            "---\nname: developer\ndescription: dev\nextends: base-harness-template\n---\n"
            "FRAGMENT BODY\n"
        )
        result = render_mod.compose_profile(self.BASE, fragment)
        parsed = parse_agent_profile_text(result, "developer")
        assert parsed.name == "developer"
        assert parsed.description == "dev"

    def test_extends_key_is_stripped_from_result(self):
        fragment = (
            "---\nname: developer\ndescription: dev\nextends: base-harness-template\n---\n"
            "FRAGMENT BODY\n"
        )
        result = render_mod.compose_profile(self.BASE, fragment)
        assert "extends" not in result

    def test_mcp_servers_inherited_when_fragment_omits_it(self):
        fragment = (
            "---\nname: developer\ndescription: dev\nextends: base-harness-template\n---\n"
            "FRAGMENT BODY\n"
        )
        result = render_mod.compose_profile(self.BASE, fragment)
        parsed = parse_agent_profile_text(result, "developer")
        assert parsed.mcpServers == {
            "cao-mcp-server": {"type": "stdio", "command": "cao-mcp-server", "args": []}
        }

    def test_mcp_servers_fragment_override_replaces_base_wholesale(self):
        fragment = (
            "---\n"
            "name: developer\ndescription: dev\nextends: base-harness-template\n"
            "mcpServers:\n"
            "  cao-mcp-server:\n"
            "    type: stdio\n"
            "    command: uvx\n"
            "    args: [--from, custom]\n"
            "---\n"
            "FRAGMENT BODY\n"
        )
        result = render_mod.compose_profile(self.BASE, fragment)
        parsed = parse_agent_profile_text(result, "developer")
        assert parsed.mcpServers["cao-mcp-server"]["command"] == "uvx"

    def test_body_is_base_then_fragment_in_order(self):
        fragment = (
            "---\nname: developer\ndescription: dev\nextends: base-harness-template\n---\n"
            "FRAGMENT BODY\n"
        )
        result = render_mod.compose_profile(self.BASE, fragment)
        parsed = parse_agent_profile_text(result, "developer")
        assert parsed.system_prompt.index("BASE BODY") < parsed.system_prompt.index("FRAGMENT BODY")


class TestTemplatePackagingParity:
    """The checked-in agent_store/*.md must always equal what rendering produces."""

    def test_base_harness_template_source_exists(self):
        assert (TEMPLATES_DIR / "base-harness-template.md").is_file()

    # Every built-in profile now extends the base template (operator directive,
    # 2026-07-17): applied universally, no per-profile/per-task-type selection --
    # diversification into specialized templates is a deliberately separate,
    # later step.
    EXTENDING_PROFILES = [
        "developer",
        "general-purpose",
        "reviewer",
        "code_supervisor",
        "workflow_scout",
        "memory_manager",
    ]

    @pytest.mark.parametrize("name", EXTENDING_PROFILES)
    def test_rendered_profile_matches_checked_in_copy(self, name):
        rendered = render_mod._render_all()
        assert name in rendered, f"{name}.md fragment missing an 'extends' key or missing entirely."
        checked_in = (AGENT_STORE_DIR / f"{name}.md").read_text(encoding="utf-8")
        assert rendered[name] == checked_in, (
            f"agent_store/{name}.md has drifted from its templates. Run `{RENDER_COMMAND}`."
        )

    @pytest.mark.parametrize("name", EXTENDING_PROFILES)
    def test_rendered_profile_carries_the_send_message_warning(self, name):
        rendered = render_mod._render_all()
        assert "mcp__cao-mcp-server__send_message" in rendered[name]
        assert "native `SendMessage`" in rendered[name]

    @pytest.mark.parametrize("name", EXTENDING_PROFILES)
    def test_rendered_profile_carries_the_ask_your_dispatcher_guidance(self, name):
        rendered = render_mod._render_all()
        assert "If a question comes up while you're working" in rendered[name]
        # Both the default-to-asking rule AND the explicit-instruction-wins escape
        # hatch must survive composition, not just one or the other.
        assert "send the question to whoever dispatched you" in rendered[name]
        assert "an explicit instruction in the task always wins over it" in rendered[name]
        # The Handoff/Assign split matters here specifically -- Handoff's dispatcher
        # is synchronously blocked and can't receive a mid-task message at all.
        assert "no one listening" in rendered[name]

    @pytest.mark.parametrize("name", EXTENDING_PROFILES)
    def test_rendered_profile_parses_and_has_mcp_server(self, name):
        rendered = render_mod._render_all()
        parsed = parse_agent_profile_text(rendered[name], name)
        assert parsed.name == name
        assert parsed.mcpServers and "cao-mcp-server" in parsed.mcpServers

    def test_every_extending_profile_exists_as_a_fragment(self):
        assert set(self.EXTENDING_PROFILES) == set(render_mod._render_all().keys())

    @pytest.mark.parametrize("name", EXTENDING_PROFILES)
    def test_fragment_does_not_duplicate_shared_base_text(self, name):
        """The source fragments (not the rendered output) must not restate the
        shared base content verbatim -- that's the actual "no duplication" claim."""
        fragment_text = (TEMPLATES_DIR / f"{name}.md").read_text(encoding="utf-8")
        assert "mcp__cao-mcp-server__send_message" not in fragment_text
        assert "list_siblings" not in fragment_text

    def test_memory_manager_overrides_the_memory_store_instruction(self):
        """memory_manager's real job conflicts with the base template's generic
        'always memory_store' guidance (rule: curation only, never store on its
        own initiative) -- its fragment must explicitly override that, not
        silently inherit a contradiction."""
        rendered = render_mod._render_all()["memory_manager"]
        assert "must NOT use `memory_store`" in rendered
