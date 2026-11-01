"""SQLite and loopback dashboard behavior without GitHub or Docker."""

from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import dashboard
import swfactory


class EventStoreTests(unittest.TestCase):
    def test_concurrent_events_and_interrupted_run_are_queryable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "git" / "swfactory" / "telemetry.sqlite3"
            store = dashboard.EventStore(path)
            run_id = store.start_run("owner/repo", "once")
            agent_id = "agent-1"
            store.record(run_id, "owner/repo", "agent_started", "controller", "planner",
                         42, 1, agent_id, "running", "Planner started",
                         {"sources": ["issue body"]})
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda index: store.record(
                    run_id, "owner/repo", "handoff", "planner", "controller",
                    42, index, None, "completed", "Result {}".format(index), {}), range(40)))
            state = store.snapshot("owner/repo")
            self.assertEqual(state["active_agents"][0]["agent_id"], agent_id)
            self.assertEqual(len([event for event in state["events"]
                                  if event["kind"] == "handoff"]), 40)
            next_run = store.start_run("owner/repo", "scan")
            state = store.snapshot("owner/repo")
            self.assertEqual(state["active_agents"], [])
            self.assertEqual(state["runs"][1]["status"], "interrupted")
            store.finish_run(next_run, "owner/repo", "completed")
            self.assertEqual(store.snapshot("owner/repo")["runs"][0]["status"], "completed")
            self.assertTrue(path.is_file())

    def test_codex_telemetry_excludes_prompt_and_error_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = dashboard.EventStore(root / "telemetry.sqlite3")
            run_id = store.start_run("owner/repo", "once")
            observer = dashboard.RunObserver(store, "owner/repo", run_id)
            with patch.object(swfactory, "OBSERVER", observer), \
                    patch("swfactory.git_common_dir", return_value=root / ".git"), \
                    patch("swfactory.codex_auth_dir", return_value=root / "auth"), \
                    patch("swfactory.codex_container_image", return_value="test-image"), \
                    patch("swfactory.run", side_effect=RuntimeError("SECRET-ERROR")):
                with swfactory.trace_scope("planner", 42, 1, ("issue body", "owner comments")):
                    with self.assertRaisesRegex(RuntimeError, "SECRET-ERROR"):
                        swfactory.codex("SECRET-PROMPT", root, read_only=True)
            serialized = json.dumps(store.snapshot("owner/repo"))
            self.assertNotIn("SECRET-PROMPT", serialized)
            self.assertNotIn("SECRET-ERROR", serialized)
            self.assertIn('"status": "failed"', serialized)
            self.assertIn('"recipient": "planner"', serialized)


class DashboardHttpTests(unittest.TestCase):
    def test_loopback_api_is_read_only_and_html_escapes_dynamic_data(self):
        with tempfile.TemporaryDirectory() as directory:
            store = dashboard.EventStore(Path(directory) / "telemetry.sqlite3")
            run_id = store.start_run("owner/repo", "once")
            store.record(run_id, "owner/repo", "handoff", "planner", "controller",
                         42, None, None, "completed", "<script>attack()</script>", {})
            server = dashboard.make_server(store, "owner/repo", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def request(method, path, host=None):
                    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    connection.request(method, path, headers={"Host": host or "localhost"})
                    response = connection.getresponse()
                    data = response.read()
                    connection.close()
                    return response.status, data

                status, body = request("GET", "/api/state")
                self.assertEqual(status, 200)
                self.assertIn("<script>attack()</script>", body.decode())
                status, html = request("GET", "/")
                self.assertEqual(status, 200)
                self.assertNotIn(b"<script>attack()", html)
                self.assertIn(b"/app.js", html)
                status, script = request("GET", "/app.js")
                self.assertEqual(status, 200)
                self.assertIn(b"textContent", script)
                self.assertEqual(request("POST", "/api/state")[0], 405)
                self.assertEqual(request("GET", "/api/state", "example.com")[0], 400)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_dashboard_command_uses_project_git_metadata_without_github_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            swfactory.run(["git", "init"], cwd=project)
            selected = swfactory.ConfigurationResult(
                swfactory.Configuration("owner/repo", "remote", None, project, root / "work"), ())

            class ShortServer:
                server_port = 12345

                def serve_forever(self):
                    return None

                def server_close(self):
                    return None

            with patch.object(sys, "argv", ["swfactory.py", "dashboard", "--port", "0"]), \
                    patch("swfactory.validate_configuration", return_value=selected), \
                    patch("swfactory.make_server", return_value=ShortServer()) as make, \
                    patch("swfactory.isolate_github_auth", side_effect=AssertionError("auth")):
                swfactory.main()
            self.assertEqual(make.call_args.args[1], "owner/repo")
            self.assertEqual(make.call_args.args[0].path,
                             (project / ".git" / "swfactory" / "telemetry.sqlite3").resolve())
            self.assertFalse((root / "work").exists())

    def test_controller_run_records_local_queue_without_changing_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, work = root / "project", root / "work"
            project.mkdir()
            work.mkdir()
            swfactory.run(["git", "init"], cwd=project)
            with patch.object(sys, "argv", ["swfactory.py"]), \
                    patch("swfactory.config", return_value=(
                        "owner/repo", "remote", root / "key", project, work)), \
                    patch("swfactory.isolate_github_auth"), \
                    patch("swfactory.verify_github_access"), \
                    patch("swfactory.owner_handle", return_value="owner"), \
                    patch("swfactory.bot_handle", return_value="bot"), \
                    patch("swfactory.ensure_labels"), \
                    patch("swfactory.reconcile_merged_issues"), \
                    patch("swfactory.resume_answered_issues"), \
                    patch("swfactory.pending_rephrase_issues", return_value=[]), \
                    patch("swfactory.pending_issues", return_value=[]):
                swfactory.main()
            path = project / ".git" / "swfactory" / "telemetry.sqlite3"
            state = dashboard.EventStore(path).snapshot("owner/repo")
            self.assertEqual(state["runs"][0]["status"], "completed")
            self.assertEqual([item["kind"] for item in reversed(state["events"])],
                             ["run_started", "queue_observed", "queue_observed", "run_finished"])
            self.assertEqual(swfactory.run(["git", "status", "--porcelain"], cwd=project), "")


if __name__ == "__main__":
    unittest.main()
