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

    def test_codex_success_links_handoff_and_elapsed_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = dashboard.EventStore(root / "telemetry.sqlite3")
            run_id = store.start_run("owner/repo", "once")
            observer = dashboard.RunObserver(store, "owner/repo", run_id)
            with patch.object(swfactory, "OBSERVER", observer), \
                    patch("swfactory.git_common_dir", return_value=root / ".git"), \
                    patch("swfactory.codex_auth_dir", return_value=root / "auth"), \
                    patch("swfactory.codex_container_image", return_value="test-image"), \
                    patch("swfactory.run", return_value=""):
                with swfactory.trace_scope("planner", 42, 1, ("issue body",),
                                           ("https://github.com/owner/repo/issues/42",)):
                    agent_id = swfactory.codex("PRIVATE PROMPT", root, read_only=True)
            events = [event for event in store.snapshot("owner/repo")["events"]
                      if event["agent_id"] == agent_id]
            self.assertEqual([event["kind"] for event in reversed(events)],
                             ["agent_started", "agent_finished"])
            self.assertEqual(events[0]["details"]["result"], "worktree")
            self.assertGreaterEqual(events[0]["details"]["elapsed_ms"], 0)
            self.assertEqual(events[1]["details"]["references"],
                             ["https://github.com/owner/repo/issues/42"])
            self.assertNotIn("PRIVATE PROMPT", json.dumps(events))

    def test_issue_workflow_links_calls_results_and_pages_long_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = dashboard.EventStore(Path(directory) / "telemetry.sqlite3")
            run_id = store.start_run("owner/repo", "once")
            record = lambda kind, actor, recipient=None, task=None, agent=None, status=None, details=None: (
                store.record(run_id, "owner/repo", kind, actor, recipient, 42, task, agent,
                             status, kind, details or {}))
            record("issue_started", "controller", "github", status="running")
            record("plan_ready", "planner", "controller", agent="plan-call",
                   status="completed", details={"task_count": 2, "plan_sha256": "abc"})
            record("subtasks_registered", "controller", "github", status="completed",
                   details={"issue_numbers": [61, 62]})
            record("agent_started", "controller", "implementer", task=1, agent="code-call",
                   status="running", details={"sources": ["task specification"],
                                              "references": ["https://github.com/owner/repo/issues/61"]})
            record("agent_finished", "implementer", "controller", task=1, agent="code-call",
                   status="completed", details={"result": "worktree", "elapsed_ms": 1275})
            record("tests_verified", "verifier", "controller", task=1, agent="verify-call",
                   status="completed", details={"test_case_count": 3,
                                                "categories": ["basic", "functional", "integration"]})
            record("performance_measurement", "benchmark", "controller", task=1,
                   status="completed", details={"metric": "latency", "target": 10,
                                                "baseline": 20, "candidate": "A", "value": 9,
                                                "unit": "ms", "attempt": 2})
            record("pr_created", "controller", "github", task=1, status="completed",
                   details={"url": "https://github.com/owner/repo/pull/77"})
            for index in range(230):
                record("progress", "controller", task=1, details={"sequence": index})
            store.record(run_id, "owner/repo", "issue_failed", "controller", None, 43,
                         None, None, "failed", "Other issue failed", {})
            latest = store.issue_list("owner/repo")
            self.assertEqual([item["issue_number"] for item in latest[:2]], [43, 42])
            self.assertEqual([item["status"] for item in latest[:2]], ["failed", "running"])
            first = store.issue_snapshot("owner/repo", 42)
            self.assertEqual(first["issue_number"], 42)
            self.assertEqual(len(first["events"]), 100)
            self.assertIsNotNone(first["next_cursor"])
            self.assertEqual(first["calls"][0]["elapsed_ms"], 1275)
            self.assertEqual(first["calls"][0]["references"],
                             ["https://github.com/owner/repo/issues/61"])
            self.assertEqual(first["performance"][0]["details"]["value"], 9)
            self.assertIn("pr_created", [item["kind"] for item in first["artifacts"]])
            self.assertEqual({item["key"]: item["status"] for item in first["stages"]}["review"],
                             "completed")
            older = store.issue_snapshot("owner/repo", 42, before=first["next_cursor"])
            self.assertLess(older["events"][0]["id"], first["events"][-1]["id"])
            self.assertEqual(store.issue_snapshot("owner/repo", 999), None)
            with self.assertRaises(ValueError):
                store.issue_snapshot("owner/repo", 0)

    def test_failure_reason_and_retry_state_are_separate_from_old_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = dashboard.EventStore(Path(directory) / "telemetry.sqlite3")
            run_id = store.start_run("owner/repo", "once")
            store.record(run_id, "owner/repo", "plan_ready", "planner", "controller",
                         8, None, "call-1", "completed", "Plan", {"task_count": 1})
            store.record(run_id, "owner/repo", "issue_failed", "controller", None,
                         8, None, None, "failed", "Issue failed",
                         {"reason_code": "required_tests_failed", "error_type": "RuntimeError"})
            failed = store.issue_snapshot("owner/repo", 8)
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["failure"]["reason_code"], "required_tests_failed")
            self.assertEqual({s["key"]: s["status"] for s in failed["stages"]}["plan"], "failed")
            store.record(run_id, "owner/repo", "issue_started", "controller", "github",
                         8, None, None, "running", "Retry", {})
            retry = store.issue_snapshot("owner/repo", 8)
            self.assertEqual(retry["status"], "running")
            self.assertIsNone(retry["failure"])


class GitHubSnapshotTests(unittest.TestCase):
    def test_existing_issues_subtasks_and_prs_are_connected_without_body_storage(self):
        client = dashboard.GitHubSnapshot("owner/repo", "/tmp/project-gh", ttl=60)
        issues = [
            {"number": 10, "title": "Parent", "state": "OPEN", "updatedAt": "2026-01-01",
             "url": "https://github.com/owner/repo/issues/10", "body": "secret prompt",
             "labels": [{"name": "accept"}]},
            {"number": 11, "title": "[#10 task 1] <script>bad()</script>", "state": "OPEN",
             "updatedAt": "2026-01-02", "url": "https://github.com/owner/repo/issues/11",
             "body": "private issue body", "labels": [{"name": "swfactory:subtask"}]},
        ]
        pulls = [{"number": 12, "title": "Implement task", "state": "OPEN",
                  "isDraft": False, "mergedAt": None, "updatedAt": "2026-01-03",
                  "url": "https://github.com/owner/repo/pull/12",
                  "closingIssuesReferences": [{"number": 11}]}]
        with patch.object(client, "_list", side_effect=[issues, pulls]) as fetch:
            result = client.snapshot()
            self.assertEqual(client.snapshot(), result)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(result["issues"][0]["children"], [11])
        self.assertEqual(result["issues"][1]["parent"], 10)
        self.assertEqual(result["issues"][1]["pull_requests"], [12])
        self.assertEqual(result["pull_requests"][0]["issues"], [11])
        self.assertNotIn("body", json.dumps(result))
        with patch.object(client, "_list", side_effect=RuntimeError("token text")):
            client.next_refresh = 0
            cached = client.snapshot()
        self.assertEqual(cached["error"], "RuntimeError")
        self.assertNotIn("token text", json.dumps(cached))
        self.assertEqual(cached["issues"][0]["children"], [11])


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
                status, issues = request("GET", "/api/issues")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(issues)[0]["issue_number"], 42)
                status, github = request("GET", "/api/github")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(github)["error"], "not_configured")
                status, detail = request("GET", "/api/issues/42")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(detail)["issue_number"], 42)
                self.assertEqual(request("GET", "/issue/42")[0], 200)
                status, detail_script = request("GET", "/detail.js")
                self.assertEqual(status, 200)
                self.assertIn(b"textContent", detail_script)
                self.assertNotIn(b"innerHTML", detail_script)
                self.assertEqual(request("GET", "/api/issues/0")[0], 404)
                self.assertEqual(request("GET", "/api/issues/42?before=bad")[0], 400)
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
                    patch("swfactory.isolate_github_auth", return_value=root / "auth") as auth:
                swfactory.main()
            self.assertEqual(make.call_args.args[1], "owner/repo")
            auth.assert_called_once_with("owner/repo")
            self.assertIsInstance(make.call_args.args[3], dashboard.GitHubSnapshot)
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
