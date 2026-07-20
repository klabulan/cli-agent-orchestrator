"""Tests for the create_sibling_session MCP tool (#303).

assign/handoff both create a worker terminal via ``_create_terminal``, which --
whenever CAO_TERMINAL_ID is set (i.e. always, for a real running agent) --
unconditionally takes the existing-session branch and sets ``caller_id`` to the
caller, making the new terminal a recorded child. There was no agent-facing path
that reaches the new-session branch (``POST /sessions``, which never accepts or
sets ``caller_id``) from inside a running terminal. ``create_sibling_session``
closes that gap: it always calls ``POST /sessions`` directly, so the result is a
genuine peer, never a child.
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
                result = _create_sibling_session_impl(
                    "developer", None, None, None, 200
                )

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
            result = _create_sibling_session_impl("developer", None, None, None, 200)

        assert result["success"] is True
        assert result["terminal_id"] == "sib-123"
        assert "peer" in result["message"]
        assert "child" not in result["message"] or "not appear as your child" in result["message"]

        mock_post.assert_called_once()
        call = mock_post.call_args
        assert call.args[0] == "http://127.0.0.1:9889/sessions"
        params = call.kwargs["params"]
        assert params["agent_profile"] == "developer"
        assert params["working_directory"] == "/repo/project5/folder12"
        # The whole point of #303: no caller_id anywhere in the request.
        assert "caller_id" not in params
        assert call.kwargs["json"] == {"group": ["tenant_1", "project_5", "folder_12"]}

    @patch("cli_agent_orchestrator.mcp_server.server.requests.post")
    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_explicit_working_directory_and_group_override_inherited_ones(
        self, mock_get, mock_post
    ):
        own_terminal_resp = MagicMock()
        own_terminal_resp.raise_for_status.return_value = None
        own_terminal_resp.json.return_value = _own_terminal_response()
        mock_get.return_value = own_terminal_resp

        create_resp = MagicMock()
        create_resp.raise_for_status.return_value = None
        create_resp.json.return_value = {"id": "sib-456", "session_name": "cao-sib-2"}
        mock_post.return_value = create_resp

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "caller-abc"}):
            result = _create_sibling_session_impl(
                "analyst",
                None,
                "/repo/other-project",
                ["tenant_1", "project_9"],
                200,
            )

        assert result["success"] is True
        # working_directory was supplied explicitly, so the working-directory
        # GET for the caller's own cwd must never fire -- only the own-terminal
        # lookup (plus _get_cleanup_nudge's own unrelated GET in the success
        # message, same as _assign_impl's success path already does).
        get_urls = [call.args[0] for call in mock_get.call_args_list]
        assert "http://127.0.0.1:9889/terminals/caller-abc/working-directory" not in get_urls
        params = mock_post.call_args.kwargs["params"]
        assert params["working_directory"] == "/repo/other-project"
        assert mock_post.call_args.kwargs["json"] == {"group": ["tenant_1", "project_9"]}

    @patch("cli_agent_orchestrator.mcp_server.server.requests.post")
    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_empty_group_override_opts_out_of_discovery(self, mock_get, mock_post):
        own_terminal_resp = MagicMock()
        own_terminal_resp.raise_for_status.return_value = None
        own_terminal_resp.json.return_value = _own_terminal_response()
        mock_get.return_value = own_terminal_resp

        create_resp = MagicMock()
        create_resp.raise_for_status.return_value = None
        create_resp.json.return_value = {"id": "sib-789", "session_name": "cao-sib-3"}
        mock_post.return_value = create_resp

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "caller-abc"}):
            _create_sibling_session_impl("developer", None, "/repo/x", [], 200)

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
        mock_get.return_value = own_terminal_resp

        create_resp = MagicMock()
        create_resp.raise_for_status.return_value = None
        create_resp.json.return_value = {"id": "sib-999", "session_name": "cao-sib-4"}
        mock_post.return_value = create_resp

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "caller-abc"}):
            result = _create_sibling_session_impl(
                "developer", "start working on X", "/repo/x", None, 200
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
        mock_get.return_value = own_terminal_resp

        mock_post.side_effect = requests.Timeout("timed out")

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "caller-abc"}):
            result = _create_sibling_session_impl("developer", None, "/repo/x", None, 42)

        assert result["success"] is False
        assert "timed out after 42s" in result["message"]

    @patch("cli_agent_orchestrator.mcp_server.server.requests.post")
    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_http_error_surfaces_server_detail(self, mock_get, mock_post):
        own_terminal_resp = MagicMock()
        own_terminal_resp.raise_for_status.return_value = None
        own_terminal_resp.json.return_value = _own_terminal_response()
        mock_get.return_value = own_terminal_resp

        error_response = MagicMock()
        error_response.json.return_value = {"detail": "invalid working_directory"}
        http_error = requests.HTTPError("400 Client Error")
        http_error.response = error_response
        post_response = MagicMock()
        post_response.raise_for_status.side_effect = http_error
        mock_post.return_value = post_response

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "caller-abc"}):
            result = _create_sibling_session_impl("developer", None, "/nope", None, 200)

        assert result["success"] is False
        assert "invalid working_directory" in result["message"]
