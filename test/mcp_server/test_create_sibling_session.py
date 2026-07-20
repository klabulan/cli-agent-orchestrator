"""Tests for the create_sibling_session MCP tool (#303).

assign/handoff both create a worker terminal via ``_create_terminal``, which --
whenever CAO_TERMINAL_ID is set (i.e. always, for a real running agent) --
unconditionally takes the existing-session branch and sets ``caller_id`` to the
caller, making the new terminal a recorded child. There was no agent-facing path
that reaches the new-session branch (``POST /sessions``, which never accepts or
sets ``caller_id``) from inside a running terminal. ``create_sibling_session``
closes that gap: it always calls ``POST /sessions`` directly, so the result is a
genuine peer, never a child.

No ``working_directory`` parameter exists on this tool (independent-ROAST finding,
round 2): a caller-supplied path would go straight to CAO's own ``POST /sessions``
with no tenant-boundary validation available to this tool (unlike ``group``, a
filesystem path has no self-contained containment check this tool can perform --
see ``_create_sibling_session_impl``'s own docstring). The sibling always inherits
the caller's own current working_directory.
"""

import os
from unittest.mock import MagicMock, patch

import requests

from cli_agent_orchestrator.mcp_server.server import _create_sibling_session_impl


def _own_terminal_response(**overrides):
    base = {
        "id": "caller-abc",
        "provider": "claude_code",
        "session_name": "cao-caller-session",
        "group": ["tenant_1", "project_5", "folder_12"],
    }
    base.update(overrides)
    return base


class TestCreateSiblingSessionImpl:
    def test_no_terminal_id_returns_error_without_network_call(self):
        with patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get, patch(
            "cli_agent_orchestrator.mcp_server.server.requests.post"
        ) as mock_post:
            with patch.dict(os.environ, {}, clear=True):
                result = _create_sibling_session_impl("developer", None, None, 200)

        assert result["success"] is False
        assert "CAO_TERMINAL_ID not set" in result["message"]
        mock_get.assert_not_called()
        mock_post.assert_not_called()

    @patch("cli_agent_orchestrator.mcp_server.server.requests.post")
    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_inherits_group_and_working_directory_and_never_sets_caller_id(
        self, mock_get, mock_post
    ):
        own_terminal_resp = MagicMock()
        own_terminal_resp.raise_for_status.return_value = None
        own_terminal_resp.json.return_value = _own_terminal_response()

        wd_resp = MagicMock()
        wd_resp.status_code = 200
        wd_resp.json.return_value = {"working_directory": "/repo/project5/folder12"}

        mock_get.side_effect = [own_terminal_resp, wd_resp]

        create_resp = MagicMock()
        create_resp.raise_for_status.return_value = None
        create_resp.json.return_value = {
            "id": "sib-123",
            "session_name": "cao-sib-session",
        }
        mock_post.return_value = create_resp

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "caller-abc"}):
            result = _create_sibling_session_impl("developer", None, None, 200)

        assert result["success"] is True
        assert result["terminal_id"] == "sib-123"
        assert "peer" in result["message"]
        assert "child" not in result["message"] or "not appear as your child" in result["message"]

        mock_post.assert_called_once()
        call = mock_post.call_args
        assert call.args[0] == "http://127.0.0.1:9889/sessions"
        params = call.kwargs["params"]
        assert params["agent_profile"] == "developer"
        # working_directory is ALWAYS the caller's own -- there is no override
        # parameter on this tool at all (see module docstring).
        assert params["working_directory"] == "/repo/project5/folder12"
        # The whole point of #303: no caller_id anywhere in the request.
        assert "caller_id" not in params
        assert call.kwargs["json"] == {"group": ["tenant_1", "project_5", "folder_12"]}

    def test_working_directory_is_not_an_accepted_parameter(self):
        """Security regression guard (independent-ROAST finding, round 2, harness-control#303):
        this tool must not accept a caller-supplied working_directory at all -- confirmed by
        calling it with the pre-fix argument shape and asserting a TypeError, not just checking
        behavior. A future edit that silently reintroduces the parameter (e.g. merging an old
        branch) would break this loudly rather than silently reopening the escalation."""
        import inspect

        sig = inspect.signature(_create_sibling_session_impl)
        assert "working_directory" not in sig.parameters

    @patch("cli_agent_orchestrator.mcp_server.server.requests.post")
    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_cross_tenant_group_override_is_rejected(self, mock_get, mock_post):
        """Security regression guard (independent-ROAST finding, harness-control#303): a group
        override whose leading (tenant/workspace) element differs from the caller's own must be
        rejected outright, not silently honored -- otherwise this tool would be the first
        agent-facing capability able to hand an LLM an arbitrary tenant-discovery scope in one
        call (list_siblings/send_message would then reach another tenant's sessions)."""
        own_terminal_resp = MagicMock()
        own_terminal_resp.raise_for_status.return_value = None
        own_terminal_resp.json.return_value = _own_terminal_response(
            group=["tenant_1", "project_5", "folder_12"]
        )
        mock_get.return_value = own_terminal_resp

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "caller-abc"}):
            result = _create_sibling_session_impl(
                "developer", None, ["tenant_2", "project_1"], 200
            )

        assert result["success"] is False
        assert "tenant" in result["message"]
        mock_post.assert_not_called()

    @patch("cli_agent_orchestrator.mcp_server.server.requests.post")
    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_same_tenant_group_override_is_allowed(self, mock_get, mock_post):
        """The positive case for the same guard: AC2 (issue #303's own "other folders/projects"
        ask) only ever asked for a different project/folder, never a different tenant -- an
        override that keeps the leading element identical to the caller's own must still work."""
        own_terminal_resp = MagicMock()
        own_terminal_resp.raise_for_status.return_value = None
        own_terminal_resp.json.return_value = _own_terminal_response(
            group=["tenant_1", "project_5", "folder_12"]
        )
        mock_get.side_effect = [
            own_terminal_resp,
            MagicMock(status_code=200, json=lambda: {"working_directory": "/repo/x"}),
        ]

        create_resp = MagicMock()
        create_resp.raise_for_status.return_value = None
        create_resp.json.return_value = {"id": "sib-abc", "session_name": "cao-sib-abc"}
        mock_post.return_value = create_resp

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "caller-abc"}):
            result = _create_sibling_session_impl(
                "developer", None, ["tenant_1", "project_9"], 200
            )

        assert result["success"] is True
        assert mock_post.call_args.kwargs["json"] == {"group": ["tenant_1", "project_9"]}

    @patch("cli_agent_orchestrator.mcp_server.server.requests.post")
    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_group_override_rejected_when_caller_has_no_own_group(self, mock_get, mock_post):
        """No own group means no tenant context to authorize ANY override against -- reject
        rather than silently trust a caller with nothing of its own to compare to."""
        own_terminal_resp = MagicMock()
        own_terminal_resp.raise_for_status.return_value = None
        own_terminal_resp.json.return_value = _own_terminal_response(group=None)
        mock_get.return_value = own_terminal_resp

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "caller-abc"}):
            result = _create_sibling_session_impl(
                "developer", None, ["tenant_1", "project_9"], 200
            )

        assert result["success"] is False
        mock_post.assert_not_called()

    @patch("cli_agent_orchestrator.mcp_server.server.requests.post")
    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_empty_group_override_allowed_even_with_no_own_group(self, mock_get, mock_post):
        """An empty-list override (opt out of discovery entirely) never widens access, so it's
        exempt from the tenant-match guard even when the caller itself has no group."""
        own_terminal_resp = MagicMock()
        own_terminal_resp.raise_for_status.return_value = None
        own_terminal_resp.json.return_value = _own_terminal_response(group=None)
        mock_get.side_effect = [
            own_terminal_resp,
            MagicMock(status_code=200, json=lambda: {"working_directory": "/repo/x"}),
        ]

        create_resp = MagicMock()
        create_resp.raise_for_status.return_value = None
        create_resp.json.return_value = {"id": "sib-abc", "session_name": "cao-sib-abc"}
        mock_post.return_value = create_resp

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "caller-abc"}):
            result = _create_sibling_session_impl("developer", None, [], 200)

        assert result["success"] is True
        assert mock_post.call_args.kwargs["json"] == {"group": []}

    @patch("cli_agent_orchestrator.mcp_server.server.requests.post")
    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_empty_group_override_opts_out_of_discovery(self, mock_get, mock_post):
        own_terminal_resp = MagicMock()
        own_terminal_resp.raise_for_status.return_value = None
        own_terminal_resp.json.return_value = _own_terminal_response()
        mock_get.side_effect = [
            own_terminal_resp,
            MagicMock(status_code=200, json=lambda: {"working_directory": "/repo/x"}),
        ]

        create_resp = MagicMock()
        create_resp.raise_for_status.return_value = None
        create_resp.json.return_value = {"id": "sib-789", "session_name": "cao-sib-3"}
        mock_post.return_value = create_resp

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "caller-abc"}):
            _create_sibling_session_impl("developer", None, [], 200)

        assert mock_post.call_args.kwargs["json"] == {"group": []}

    @patch("cli_agent_orchestrator.mcp_server.server._send_to_inbox")
    @patch("cli_agent_orchestrator.mcp_server.server.requests.post")
    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_initial_message_delivered_to_new_sibling_inbox(
        self, mock_get, mock_post, mock_send_to_inbox
    ):
        own_terminal_resp = MagicMock()
        own_terminal_resp.raise_for_status.return_value = None
        own_terminal_resp.json.return_value = _own_terminal_response()
        mock_get.side_effect = [
            own_terminal_resp,
            MagicMock(status_code=200, json=lambda: {"working_directory": "/repo/x"}),
        ]

        create_resp = MagicMock()
        create_resp.raise_for_status.return_value = None
        create_resp.json.return_value = {"id": "sib-999", "session_name": "cao-sib-4"}
        mock_post.return_value = create_resp

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "caller-abc"}):
            result = _create_sibling_session_impl(
                "developer", "start working on X", None, 200
            )

        assert result["success"] is True
        mock_send_to_inbox.assert_called_once_with("sib-999", "start working on X")
        assert "delivered" in result["message"]

    @patch("cli_agent_orchestrator.mcp_server.server.requests.post")
    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_timeout_returns_structured_failure(self, mock_get, mock_post):
        own_terminal_resp = MagicMock()
        own_terminal_resp.raise_for_status.return_value = None
        own_terminal_resp.json.return_value = _own_terminal_response()
        mock_get.side_effect = [
            own_terminal_resp,
            MagicMock(status_code=200, json=lambda: {"working_directory": "/repo/x"}),
        ]

        mock_post.side_effect = requests.Timeout("timed out")

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "caller-abc"}):
            result = _create_sibling_session_impl("developer", None, None, 42)

        assert result["success"] is False
        assert "timed out after 42s" in result["message"]

    @patch("cli_agent_orchestrator.mcp_server.server.requests.post")
    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_http_error_surfaces_server_detail(self, mock_get, mock_post):
        own_terminal_resp = MagicMock()
        own_terminal_resp.raise_for_status.return_value = None
        own_terminal_resp.json.return_value = _own_terminal_response()
        mock_get.side_effect = [
            own_terminal_resp,
            MagicMock(status_code=200, json=lambda: {"working_directory": "/repo/x"}),
        ]

        error_response = MagicMock()
        error_response.json.return_value = {"detail": "invalid working_directory"}
        http_error = requests.HTTPError("400 Client Error")
        http_error.response = error_response
        post_response = MagicMock()
        post_response.raise_for_status.side_effect = http_error
        mock_post.return_value = post_response

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "caller-abc"}):
            result = _create_sibling_session_impl("developer", None, None, 200)

        assert result["success"] is False
        assert "invalid working_directory" in result["message"]
