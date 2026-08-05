"""Tests for the create_sibling_session MCP tool.

assign/handoff both create a worker terminal via ``_create_terminal``, which --
whenever CAO_TERMINAL_ID is set (i.e. always, for a real running agent) --
unconditionally takes the existing-session branch and sets ``caller_id`` to the
caller, making the new terminal a recorded child. There was no agent-facing path
that reaches the new-session branch (``POST /sessions``, which never accepts or
sets ``caller_id``) from inside a running terminal. ``create_sibling_session``
closes that gap: it always calls ``POST /sessions`` directly, so the result is a
genuine peer, never a child.

No ``working_directory`` parameter exists on this tool: a caller-supplied path
would go straight to CAO's own ``POST /sessions`` with no validation. The
sibling always inherits the caller's own current working_directory. Consumers
that need tenant/access-boundary-aware placement should enforce it at their own
integration layer -- this tool has no concept of "tenant".
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
        "group": ["project_5", "folder_12"],
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
        # The whole point of this tool: no caller_id anywhere in the request.
        assert "caller_id" not in params
        assert call.kwargs["json"] == {"group": ["project_5", "folder_12"]}

    def test_working_directory_is_not_an_accepted_parameter(self):
        """Regression guard: this tool must not accept a caller-supplied
        working_directory at all -- confirmed by inspecting the real function
        signature, not just checking behavior. A future edit that silently
        reintroduces the parameter would break this loudly."""
        import inspect

        sig = inspect.signature(_create_sibling_session_impl)
        assert "working_directory" not in sig.parameters

    @patch("cli_agent_orchestrator.mcp_server.server.requests.post")
    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_explicit_group_override_is_passed_through(self, mock_get, mock_post):
        """This generic tool applies no policy of its own to an explicit `group`
        override -- it is trusted and passed straight through. Consumers that
        need to bound the override (e.g. a multi-tenant deployment) do so at
        their own integration layer, not inside this tool."""
        own_terminal_resp = MagicMock()
        own_terminal_resp.raise_for_status.return_value = None
        own_terminal_resp.json.return_value = _own_terminal_response(
            group=["project_5", "folder_12"]
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
            result = _create_sibling_session_impl("developer", None, ["other_project"], 200)

        assert result["success"] is True
        assert mock_post.call_args.kwargs["json"] == {"group": ["other_project"]}

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
            result = _create_sibling_session_impl("developer", None, [], 200)

        assert result["success"] is True
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
