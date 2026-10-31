import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import swfactory


def issue(number, *labels):
    return {"number": number, "title": str(number), "body": "", "labels": [
        {"name": label} for label in labels]}


class QueueTests(unittest.TestCase):
    @patch("swfactory.gh")
    def test_only_unprocessed_accepted_issues_in_number_order(self, gh):
        gh.return_value = json.dumps([
            issue(12, "accept", "rephased"), issue(2, "accept", "done"),
            issue(9, "accept", "swfactory:failed"), issue(4, "accept", "rephased"),
            issue(3, "accept", "swfactory:running"), issue(5, "accept", "swfactory:waiting"),
            issue(6, "accept"), issue(8, "accept", "rephased"),
        ])
        self.assertEqual([item["number"] for item in swfactory.pending_issues("owner/repo")], [4, 8, 12])
        gh.assert_called_once_with("owner/repo", "issue", "list", "--state", "open", "--label",
                                   "accept", "--limit", "1000", "--json", "number,title,body,labels")


class PlanTests(unittest.TestCase):
    def test_controller_owned_pr_checks_are_not_sent_to_local_verifier(self):
        checks = ["Run python3 -m unittest discover -s tests -v",
                  "Before retrying, read PR #19 state, head, base and title",
                  "gh pr view 19 --json state",
                  "Run doctor with invalid config and inspect exit code"]
        self.assertEqual(swfactory.local_integration_checks(checks), [checks[0], checks[3]])

    def test_plan_has_required_issue_sections(self):
        plan = {"summary": "Feature", "why": "Needed", "tasks": [
            {"what": "Parse", "why": "Input", "how": "Validate and parse", "tests": "Invalid input"}],
            "integration_tests": ["End-to-end command"]}
        swfactory.valid_plan(plan)
        body = swfactory.format_plan(plan)
        for text in ("0. 무엇인가", "1. 왜 해야 하는가", "방법 및 구현 정책", "테스트:", "통합 테스트"):
            self.assertIn(text, body)

    def test_rejects_incomplete_plan(self):
        with self.assertRaises(RuntimeError):
            swfactory.valid_plan({"tasks": [], "integration_tests": ["test"]})

    @patch("swfactory.run")
    def test_verification_requires_added_cases_and_all_test_categories(self, run):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            (worktree / "test_feature.py").write_text("def test_feature(): pass\n")
            run.side_effect = ["test_feature.py", ""]
            result = {
                "passed": True,
                "test_cases": [{"task": 1, "path": "test_feature.py", "case": "test_feature",
                                "behavior": "Feature returns expected result"}],
                "test_results": [
                    {"kind": kind, "command": "pytest -q", "output": "1 passed", "passed": True}
                    for kind in ("basic", "functional", "integration")],
                "details": "All checks passed",
            }
            swfactory.validate_verification(result, worktree, 1)
            result["test_cases"][0]["case"] = "test_feature.FeatureTests.test_feature"
            run.side_effect = ["test_feature.py", ""]
            swfactory.validate_verification(result, worktree, 1)
            report = swfactory.format_test_report(result)
            self.assertIn("Task 1: `test_feature.py`", report)
            self.assertIn("functional: `pytest -q` — PASS; 1 passed", report)
            run.side_effect = ["test_feature.py", ""]
            with self.assertRaisesRegex(RuntimeError, "Every task in this PR"):
                swfactory.validate_verification(result, worktree, 2)
            run.side_effect = ["test_feature.py", ""]
            result["test_results"] = result["test_results"][:-1]
            with self.assertRaisesRegex(RuntimeError, "Basic, functional, and integration"):
                swfactory.validate_verification(result, worktree, 1)

    @patch("swfactory.run")
    def test_verification_rejects_case_not_in_changed_file(self, run):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            (worktree / "existing_test.py").write_text("def test_existing(): pass\n")
            run.side_effect = ["feature.py", ""]
            result = {"passed": True, "test_cases": [{"task": 1, "path": "existing_test.py",
                        "case": "test_existing", "behavior": "Old behavior"}],
                      "test_results": [], "details": "Reported only existing tests"}
            with self.assertRaisesRegex(RuntimeError, "changed test file"):
                swfactory.validate_verification(result, worktree, 1)

    def test_work_record_uses_pr_number_and_links_issues(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            (worktree / "AGENTS.md").write_text(
                "Record work-items.md and docs/{work number}.md for each change.\n")
            plan = {"summary": "Feature", "why": "Needed", "tasks": [{
                "what": "Implement check", "why": "Reliability", "how": "Validate config",
                "tests": "Check valid and invalid config"}], "integration_tests": ["CLI smoke"]}
            self.assertTrue(swfactory.write_pr_work_item(
                worktree, "https://github.com/owner/repo/pull/23", 14, [15],
                "Add doctor", plan, "## Test results\nPASS"))
            record = (worktree / "docs/23.md").read_text()
            self.assertIn("# Work #23", record)
            self.assertIn("/issues/14", record)
            self.assertIn("/issues/15", record)
            self.assertIn("PASS", record)
            self.assertIn("| 23 |", (worktree / "work-items.md").read_text())
            self.assertFalse((worktree / "docs/15.md").exists())
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                swfactory.write_pr_work_item(worktree, "https://github.com/owner/repo/pull/23",
                                              14, [15], "Add doctor", plan, "PASS")
            self.assertTrue(swfactory.write_pr_work_item(
                worktree, "https://github.com/owner/repo/pull/24", 14, [],
                "Single task", plan, "PASS\n"))
            self.assertIn("Sub-issues: None\n", (worktree / "docs/24.md").read_text())
            self.assertFalse(any(line.endswith(" ") for line in
                                 (worktree / "docs/24.md").read_text().splitlines()))

    @patch("swfactory.run")
    def test_repo_view_passes_repository_as_argument(self, run):
        run.return_value = '{"defaultBranchRef":{"name":"main"}}'
        self.assertEqual(swfactory.default_branch("owner/repo"), "main")
        run.assert_called_once_with(["gh", "repo", "view", "owner/repo", "--json", "defaultBranchRef"])

    def test_repository_override_selects_another_project(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "key"
            key.write_text("test")
            project = Path(directory) / "project"
            project.mkdir()
            swfactory.run(["git", "init"], cwd=project)
            swfactory.run(["git", "remote", "add", "origin", "git@github.com:owner/target.git"], cwd=project)
            work_dir = Path(directory) / "work"
            env = Path(directory) / "env"
            env.write_text("GITHUB_SSH_KEY_PATH={}\nGITHUB_REPOGITORY=git@github.com:owner/controller.git\nPROJECT_PATH={}\nWORK_DIR={}\n".format(key, project, work_dir))
            with patch.object(swfactory, "ROOT", Path(directory)):
                repo, remote, chosen_key, chosen_project, chosen_work_dir = swfactory.config("git@github.com:owner/target.git")
            self.assertEqual((repo, remote, chosen_key, chosen_project, chosen_work_dir),
                             ("owner/target", "git@github.com:owner/target.git", key,
                              project.resolve(), work_dir.resolve()))
            self.assertTrue(work_dir.is_dir())
            env.write_text(env.read_text().replace("WORK_DIR={}\n".format(work_dir),
                                                   "WORK_DIR={}\n".format(project / "nested-work")))
            with patch.object(swfactory, "ROOT", Path(directory)):
                with self.assertRaisesRegex(RuntimeError, "outside PROJECT_PATH"):
                    swfactory.config("git@github.com:owner/target.git")
            env.write_text(env.read_text().split("WORK_DIR=")[0])
            with patch.object(swfactory, "ROOT", Path(directory)):
                self.assertEqual(swfactory.config("git@github.com:owner/target.git")[4],
                                 Path("/tmp").resolve())

    def test_github_auth_is_isolated_for_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(swfactory.Path, "home", return_value=Path(directory)):
                with patch.dict(os.environ, {"GH_TOKEN": "other-account",
                                          "GITHUB_TOKEN": "other-account"}):
                    selected = swfactory.isolate_github_auth("owner/repo")
                    self.assertEqual(os.environ["GH_CONFIG_DIR"], str(selected))
                    self.assertNotIn("GH_TOKEN", os.environ)
                    self.assertNotIn("GITHUB_TOKEN", os.environ)
                    self.assertTrue(selected.is_dir())

    @patch("swfactory.run")
    def test_github_access_requires_issue_write_permission(self, run):
        run.side_effect = ["", json.dumps({"permissions": {"pull": True, "push": False,
                                                       "triage": False}})]
        with self.assertRaisesRegex(RuntimeError, "lacks issue write access"):
            swfactory.verify_github_access("owner/repo")


class ConfigurationTests(unittest.TestCase):
    def test_missing_env_reports_each_required_setting_without_running_git(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(swfactory, "ROOT", Path(directory)), patch("swfactory.run") as run:
                result = swfactory.validate_configuration()
        self.assertEqual([finding.name for finding in result.findings],
                         ["env", "GITHUB_REPOGITORY", "GITHUB_SSH_KEY_PATH", "PROJECT_PATH"])
        self.assertNotIn(directory, repr(result.findings))
        run.assert_not_called()

    def test_invalid_settings_are_independent_and_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "env").write_text(
                "GITHUB_REPOGITORY=secret-invalid-remote\n"
                "GITHUB_SSH_KEY_PATH=secret-missing-key\n"
                "PROJECT_PATH=secret-missing-project\n"
                "WORK_DIR=\n")
            with patch.object(swfactory, "ROOT", root):
                result = swfactory.validate_configuration()
                self.assertEqual([finding.name for finding in result.findings],
                                 ["GITHUB_REPOGITORY", "GITHUB_SSH_KEY_PATH", "PROJECT_PATH"])
                for secret in ("secret-invalid-remote", "secret-missing-key", "secret-missing-project"):
                    self.assertNotIn(secret, repr(result.findings))
                (root / "env").write_text("GITHUB_REPOGITORY=git@github.com:owner/repo.git\n")
                self.assertEqual([finding.name for finding in swfactory.validate_configuration().findings],
                                 ["GITHUB_SSH_KEY_PATH", "PROJECT_PATH"])

    def test_each_missing_required_value_has_a_named_finding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / "key"
            key.touch()
            project = root / "project"
            project.mkdir()
            swfactory.run(["git", "init"], cwd=project)
            swfactory.run(["git", "remote", "add", "origin", "git@github.com:owner/repo.git"], cwd=project)
            settings = {"GITHUB_REPOGITORY": "git@github.com:owner/repo.git",
                        "GITHUB_SSH_KEY_PATH": str(key), "PROJECT_PATH": str(project)}
            with patch.object(swfactory, "ROOT", root):
                for missing in settings:
                    with self.subTest(missing=missing):
                        (root / "env").write_text("".join("{}={}\n".format(name, value)
                            for name, value in settings.items() if name != missing))
                        self.assertEqual([finding.name for finding in
                                          swfactory.validate_configuration().findings], [missing])

    def test_paths_expand_home_and_default_work_dir(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "key").touch()
            (root / "project").mkdir()
            swfactory.run(["git", "init"], cwd=root / "project")
            swfactory.run(["git", "remote", "add", "origin", "git@github.com:owner/repo.git"],
                          cwd=root / "project")
            (root / "env").write_text("GITHUB_REPOGITORY=git@github.com:owner/repo.git\n"
                                      "GITHUB_SSH_KEY_PATH=~/key\nPROJECT_PATH=~/project\n")
            with patch.object(swfactory, "ROOT", root), patch.dict(os.environ, {"HOME": str(root)}):
                result = swfactory.validate_configuration()
            self.assertEqual(result.findings, ())
            self.assertEqual(result.values.key, root / "key")
            self.assertEqual(result.values.project, (root / "project").resolve())
            self.assertEqual(result.values.work_dir, Path("/tmp").resolve())

    def test_overrides_select_valid_values_and_diagnostics_do_not_create_work_dir(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / "key"
            key.write_text("key")
            project = root / "project"
            project.mkdir()
            swfactory.run(["git", "init"], cwd=project)
            swfactory.run(["git", "remote", "add", "origin", "https://github.com/OWNER/Target.git"], cwd=project)
            work_dir = root / "new-work"
            (root / "env").write_text(
                "GITHUB_REPOGITORY=invalid\nGITHUB_SSH_KEY_PATH={}\n"
                "PROJECT_PATH=missing\nWORK_DIR={}\n".format(key, work_dir))
            with patch.object(swfactory, "ROOT", root):
                self.assertEqual([finding.name for finding in swfactory.validate_configuration(
                    "git@github.com:owner/target.git").findings], ["PROJECT_PATH"])
                self.assertEqual([finding.name for finding in swfactory.validate_configuration(
                    project_override=str(project)).findings], ["GITHUB_REPOGITORY"])
                result = swfactory.validate_configuration(
                    "git@github.com:owner/target.git", str(project))
                self.assertEqual(result.findings, ())
                self.assertFalse(work_dir.exists())
                expected = ("owner/target", "git@github.com:owner/target.git", key,
                            project.resolve(), work_dir.resolve())
                self.assertEqual((result.values.repository, result.values.remote, result.values.key,
                                  result.values.project, result.values.work_dir), expected)
                self.assertEqual(swfactory.config("git@github.com:owner/target.git", str(project)), expected)
                self.assertTrue(work_dir.is_dir())

    def test_config_hides_local_git_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / "key"
            key.touch()
            project = root / "project"
            project.mkdir()
            (root / "env").write_text("GITHUB_REPOGITORY=git@github.com:owner/repo.git\n"
                                      "GITHUB_SSH_KEY_PATH={}\nPROJECT_PATH={}\n".format(key, project))
            with patch.object(swfactory, "ROOT", root), patch("swfactory.run",
                    side_effect=RuntimeError("secret subprocess stderr")):
                with self.assertRaisesRegex(RuntimeError, "PROJECT_PATH is not a Git repository root") as error:
                    swfactory.config()
            self.assertNotIn("secret", str(error.exception))

    def test_diagnostics_report_checkout_and_origin_errors_without_git_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / "key"
            key.touch()
            project = root / "project"
            project.mkdir()
            work_dir = root / "work"
            (root / "env").write_text(
                "GITHUB_REPOGITORY=git@github.com:owner/repo.git\n"
                "GITHUB_SSH_KEY_PATH={}\nPROJECT_PATH={}\nWORK_DIR={}\n".format(
                    key, project, work_dir))
            with patch.object(swfactory, "ROOT", root):
                result = swfactory.validate_configuration()
                self.assertEqual([finding.message for finding in result.findings],
                                 ["PROJECT_PATH is not a Git repository root"])
                swfactory.run(["git", "init"], cwd=project)
                result = swfactory.validate_configuration()
                self.assertEqual([finding.message for finding in result.findings],
                                 ["PROJECT_PATH does not have an origin remote"])
                swfactory.run(["git", "remote", "add", "origin",
                               "https://github.com/other/repo.git"], cwd=project)
                result = swfactory.validate_configuration()
                self.assertEqual([finding.message for finding in result.findings],
                                 ["PROJECT_PATH origin does not match GITHUB_REPOGITORY"])
                self.assertFalse(work_dir.exists())
                with patch("swfactory.run", side_effect=RuntimeError("secret subprocess stderr")):
                    result = swfactory.validate_configuration()
                self.assertNotIn("secret", repr(result.findings))
                self.assertEqual(result.findings[0].name, "PROJECT_PATH")

    def test_work_dir_may_be_an_ancestor_of_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / "key"
            key.touch()
            project = root / "project"
            project.mkdir()
            swfactory.run(["git", "init"], cwd=project)
            swfactory.run(["git", "remote", "add", "origin", "git@github.com:owner/repo.git"], cwd=project)
            (root / "env").write_text("GITHUB_REPOGITORY=git@github.com:owner/repo.git\n"
                                      "GITHUB_SSH_KEY_PATH={}\nPROJECT_PATH={}\nWORK_DIR={}\n".format(
                                          key, project, root))
            with patch.object(swfactory, "ROOT", root):
                self.assertEqual(swfactory.validate_configuration().findings, ())


class OfflineCheckTests(unittest.TestCase):
    def fixture(self, root, *, origin="git@github.com:owner/repo.git"):
        key = root / "key"
        key.touch()
        project = root / "project"
        project.mkdir()
        swfactory.run(["git", "init"], cwd=project)
        if origin is not None:
            swfactory.run(["git", "remote", "add", "origin", origin], cwd=project)
        work_dir = root / "new-work"
        return swfactory.ConfigurationResult(
            swfactory.Configuration("owner/repo", "git@github.com:owner/repo.git",
                                    key, project, work_dir), ())

    def statuses(self, result):
        return {finding.name: finding.status for finding in result}

    def test_each_missing_executable_fails_with_remedy(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.fixture(Path(directory))
            for missing in ("git", "ssh", "gh", "codex"):
                with self.subTest(tool=missing), patch("swfactory.shutil.which",
                        side_effect=lambda name: None if name == missing else "/bin/" + name):
                    results = swfactory.check_offline_environment(config)
                    self.assertEqual(self.statuses(results)[missing], "FAIL")
                    self.assertIn("Install " + missing, next(f.action for f in results if f.name == missing))
                    self.assertEqual(self.statuses(results)["PROJECT_ROOT"],
                                     "UNKNOWN" if missing == "git" else "PASS")

    def test_key_must_be_readable_regular_file(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.fixture(Path(directory))
            values = config.values
            for key in (Path(directory) / "absent", Path(directory)):
                with self.subTest(key=key):
                    selected = swfactory.ConfigurationResult(
                        swfactory.Configuration(values.repository, values.remote, key,
                                                values.project, values.work_dir), ())
                    finding = next(f for f in swfactory.check_offline_environment(selected)
                                   if f.name == "SSH_KEY")
                    self.assertEqual(finding.status, "FAIL")
                    self.assertIn("readable regular file", finding.action)
            with patch("swfactory.os.access", return_value=False):
                finding = next(f for f in swfactory.check_offline_environment(config)
                               if f.name == "SSH_KEY")
                self.assertEqual(finding.status, "FAIL")

    def test_python_version_floor(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.fixture(Path(directory))
            with patch("swfactory.sys.version_info", (3, 6, 15)):
                finding = swfactory.check_offline_environment(config)[0]
            self.assertEqual((finding.name, finding.status), ("python", "FAIL"))
            self.assertIn("3.7", finding.action)

    def test_checkout_root_origin_and_work_dir_cases(self):
        for origin, expected_origin in (("git@github.com:owner/repo.git", "PASS"),
                                        ("https://github.com/OWNER/REPO", "PASS"),
                                        (None, "FAIL"),
                                        ("git@github.com:other/repo.git", "FAIL")):
            with self.subTest(origin=origin), tempfile.TemporaryDirectory() as directory:
                config = self.fixture(Path(directory), origin=origin)
                self.assertEqual(self.statuses(swfactory.check_offline_environment(config)),
                                 {"python": "PASS", "git": "PASS", "ssh": "PASS",
                                  "gh": "PASS", "codex": "PASS", "SSH_KEY": "PASS",
                                  "PROJECT_ROOT": "PASS", "ORIGIN": expected_origin,
                                  "WORK_DIR": "PASS"})
                self.assertFalse(config.values.work_dir.exists())
                nested = config.values.project / "nested"
                nested.mkdir()
                values = config.values
                nested_config = swfactory.ConfigurationResult(swfactory.Configuration(
                    values.repository, values.remote, values.key, nested, nested / "work"), ())
                findings = self.statuses(swfactory.check_offline_environment(nested_config))
                self.assertEqual(findings["PROJECT_ROOT"], "FAIL")
                self.assertEqual(findings["WORK_DIR"], "FAIL")

    def test_independent_failures_and_unknown_dependencies_use_local_commands_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.fixture(root)
            values = config.values
            broken = swfactory.ConfigurationResult(swfactory.Configuration(
                None, "secret-invalid-remote", root / "absent", values.project,
                values.project / "work"), ())
            real_run = swfactory.run
            with patch("swfactory.run", wraps=real_run) as run, patch(
                    "swfactory.shutil.which", side_effect=lambda name: None if name == "gh" else "/bin/" + name):
                findings = swfactory.check_offline_environment(broken)
            statuses = self.statuses(findings)
            self.assertEqual(statuses["gh"], "FAIL")
            self.assertEqual(statuses["SSH_KEY"], "FAIL")
            self.assertEqual(statuses["PROJECT_ROOT"], "PASS")
            self.assertEqual(statuses["ORIGIN"], "UNKNOWN")
            self.assertEqual(statuses["WORK_DIR"], "FAIL")
            self.assertEqual([call.args[0] for call in run.call_args_list],
                             [["git", "rev-parse", "--show-toplevel"]])
            self.assertNotIn("secret-invalid-remote", repr(findings))

    def test_valid_checkout_invokes_only_two_local_git_inspections(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.fixture(Path(directory))
            real_run = swfactory.run
            with patch("swfactory.run", wraps=real_run) as run, \
                    patch("swfactory.gh", side_effect=AssertionError("gh must not run")), \
                    patch("swfactory.codex", side_effect=AssertionError("codex must not run")), \
                    patch("swfactory.shutil.which", return_value="/usr/bin/tool"):
                findings = swfactory.check_offline_environment(config)
            self.assertTrue(all(f.status == "PASS" for f in findings))
            self.assertEqual([call.args[0] for call in run.call_args_list], [
                ["git", "rev-parse", "--show-toplevel"],
                ["git", "remote", "get-url", "origin"],
            ])
            self.assertFalse(config.values.work_dir.exists())

    def test_missing_checkout_or_git_keeps_dependent_checks_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.fixture(Path(directory))
            values = config.values
            missing_project = swfactory.ConfigurationResult(swfactory.Configuration(
                values.repository, values.remote, values.key, None, values.work_dir), ())
            with patch("swfactory.run") as run:
                findings = swfactory.check_offline_environment(missing_project)
            run.assert_not_called()
            self.assertEqual(self.statuses(findings)["PROJECT_ROOT"], "UNKNOWN")
            self.assertEqual(self.statuses(findings)["ORIGIN"], "UNKNOWN")
            self.assertEqual(self.statuses(findings)["WORK_DIR"], "UNKNOWN")
            with patch("swfactory.shutil.which",
                       side_effect=lambda name: None if name == "git" else "/bin/" + name), \
                    patch("swfactory.run") as run:
                findings = swfactory.check_offline_environment(config)
            run.assert_not_called()
            self.assertEqual(self.statuses(findings)["git"], "FAIL")
            self.assertEqual(self.statuses(findings)["SSH_KEY"], "PASS")
            self.assertEqual(self.statuses(findings)["PROJECT_ROOT"], "UNKNOWN")
            self.assertEqual(self.statuses(findings)["ORIGIN"], "UNKNOWN")
            self.assertEqual(self.statuses(findings)["WORK_DIR"], "UNKNOWN")

class ScanTests(unittest.TestCase):
    @patch("swfactory.gh")
    @patch("swfactory.codex")
    def test_scan_creates_unaccepted_nonduplicate_issue(self, codex, gh):
        proposals = {"issues": [
            {"title": "Existing", "kind": "bug", "what": "A", "why": "B", "how": "C", "tests": "D"},
            {"title": "New finding", "kind": "refactor", "what": "A", "why": "B", "how": "C", "tests": "D"},
        ]}

        def write_output(_prompt, _cwd, output_path, _schema_path, read_only):
            self.assertTrue(read_only)
            output_path.write_text(json.dumps(proposals))

        codex.side_effect = write_output
        gh.side_effect = [json.dumps([{"title": "Existing"}]), "https://github.com/owner/repo/issues/2"]
        with tempfile.TemporaryDirectory() as directory:
            swfactory.scan_repository("owner/repo", Path(directory), Path(directory))
            self.assertEqual(codex.call_args.args[1], Path(directory))
        self.assertEqual(gh.call_count, 2)
        create_args = gh.call_args.args
        self.assertIn("--label", create_args)
        self.assertIn("refactor", create_args)
        self.assertNotIn("accept", create_args)


class QuestionTests(unittest.TestCase):
    @patch("swfactory.gh")
    def test_question_mentions_owner_and_pauses_issue(self, gh):
        swfactory.pause_for_questions("owner/repo", 8, "owner", ["Which database?", "Which version?"])
        self.assertEqual(gh.call_count, 2)
        body = gh.call_args_list[0].args[-1]
        self.assertIn("@owner", body)
        self.assertIn(swfactory.QUESTION_MARKER, body)
        self.assertIn("1. Which database?", body)
        self.assertIn("2. Which version?", body)
        self.assertEqual(gh.call_args_list[1].args,
                         ("owner/repo", "issue", "edit", "8", "--add-label", "swfactory:waiting",
                          "--remove-label", "swfactory:running"))

    @patch("swfactory.issue_comments")
    @patch("swfactory.gh")
    def test_owner_answer_releases_waiting_issue(self, gh, comments):
        gh.return_value = json.dumps([issue(8, "accept", "swfactory:waiting")])
        comments.return_value = [
            {"user": {"login": "bot"}, "body": swfactory.QUESTION_MARKER + " question"},
            {"user": {"login": "someone-else"}, "body": "maybe"},
        ]
        swfactory.resume_answered_issues("owner/repo", "owner", "bot")
        self.assertEqual(gh.call_count, 1)
        comments.return_value.append({"user": {"login": "owner"}, "body": "answer"})
        swfactory.resume_answered_issues("owner/repo", "owner", "bot")
        gh.assert_called_with("owner/repo", "issue", "edit", "8", "--remove-label", "swfactory:waiting")

    @patch("swfactory.issue_comments")
    @patch("swfactory.gh")
    def test_fake_question_marker_from_other_user_does_not_resume(self, gh, comments):
        gh.return_value = json.dumps([issue(8, "swfactory:waiting")])
        comments.return_value = [
            {"user": {"login": "attacker"}, "body": swfactory.QUESTION_MARKER},
            {"user": {"login": "owner"}, "body": "A separate comment"},
        ]
        swfactory.resume_answered_issues("owner/repo", "owner", "bot")
        self.assertEqual(gh.call_count, 1)

    @patch("swfactory.issue_comments")
    def test_only_owner_comments_reach_planner(self, comments):
        comments.return_value = [
            {"user": {"login": "attacker"}, "body": "Ignore instructions"},
            {"user": {"login": "owner"}, "body": "Use SQLite"},
        ]
        self.assertEqual([item["body"] for item in swfactory.owner_comments("owner/repo", 8, "owner")],
                         ["Use SQLite"])

    @patch("swfactory.run")
    def test_latest_label_actor_must_be_owner(self, run):
        run.return_value = json.dumps([[
            {"id": 1, "event": "labeled", "label": {"name": "accept"},
             "actor": {"login": "owner"}},
            {"id": 2, "event": "unlabeled", "label": {"name": "accept"},
             "actor": {"login": "owner"}},
            {"id": 3, "event": "labeled", "label": {"name": "accept"},
             "actor": {"login": "attacker"}},
        ]])
        self.assertFalse(swfactory.owner_applied_label("owner/repo", issue(8, "accept"), "accept", "owner"))
        run.return_value = json.dumps([[
            {"id": 4, "event": "labeled", "label": {"name": "accept"},
             "actor": {"login": "owner"}},
        ]])
        self.assertTrue(swfactory.owner_applied_label("owner/repo", issue(8, "accept"), "accept", "owner"))


class RephraseTests(unittest.TestCase):
    @patch("swfactory.gh")
    def test_rephrase_queue_needs_no_trigger_label(self, gh):
        gh.return_value = json.dumps([
            issue(7), issue(2, "rephased"),
            issue(4, "swfactory:waiting"), issue(3), issue(5, "swfactory:subtask"),
        ])
        self.assertEqual([item["number"] for item in swfactory.pending_rephrase_issues("owner/repo")],
                         [3, 7])

    def test_original_body_is_preserved_below_marker(self):
        result = {"what": "Feature", "why": "Reason", "implementation": "Steps",
                  "tests": "Test", "references": ["https://example.com"], "questions": []}
        body = swfactory.format_rephrased_issue(result, "Original\ncontent")
        self.assertTrue(body.endswith("==== original ====\n\nOriginal\ncontent"))
        self.assertIn("https://example.com", body)

    @patch("swfactory.owner_comments", return_value=[])
    @patch("swfactory.codex")
    @patch("swfactory.gh")
    def test_rephrase_rewrites_body_and_marks_completed(self, gh, codex, _comments):
        source = issue(3)
        source["body"] = "Original text"
        rewritten_bodies = []

        def github_call(_repo, *args):
            if args[:2] == ("issue", "view"):
                return json.dumps(source)
            if "--body-file" in args:
                rewritten_bodies.append(Path(args[-1]).read_text())
            return ""

        gh.side_effect = github_call

        def write_result(_prompt, _checkout, output_path, _schema_path, read_only):
            self.assertTrue(read_only)
            output_path.write_text(json.dumps({"what": "Feature", "why": "Reason",
                                               "implementation": "Steps", "tests": "Test",
                                               "references": ["https://example.com"], "questions": []}))

        codex.side_effect = write_result
        with tempfile.TemporaryDirectory() as directory:
            swfactory.rephrase_issue("owner/repo", Path(directory), Path(directory), "owner", source)
        calls = [call.args for call in gh.call_args_list]
        self.assertTrue(rewritten_bodies[0].endswith("==== original ====\n\nOriginal text"))
        self.assertIn(("owner/repo", "issue", "edit", "3", "--add-label", "rephased"), calls)


class SubtaskTests(unittest.TestCase):
    def test_parent_body_links_subtasks_without_changing_original(self):
        tasks = [{"what": "Parse input\n safely"}, {"what": "Report failures"}]
        original = "First line\n\nSecond line\n"
        body = "## Specification\nDetails\n\n==== original ====\n\n" + original
        updated = swfactory.parent_body_with_subtask_links(body, "owner/repo", tasks, [7, 8])
        self.assertIn("[#7](https://github.com/owner/repo/issues/7) — Parse input safely", updated)
        self.assertIn("[#8](https://github.com/owner/repo/issues/8) — Report failures", updated)
        self.assertTrue(updated.endswith("==== original ====\n\n" + original))
        self.assertEqual(swfactory.parent_body_with_subtask_links(updated, "owner/repo", tasks, [7, 8]),
                         updated)
        revised = swfactory.parent_body_with_subtask_links(updated, "owner/repo", tasks, [9, 8])
        self.assertNotIn("issues/7", revised)
        self.assertEqual(revised.count(swfactory.SUBTASK_LINKS_START), 1)

    @patch("swfactory.gh")
    def test_parent_body_is_written_only_when_links_change(self, gh):
        tasks = [{"what": "Parse input"}]
        body = "Spec\n\n==== original ====\n\nOriginal"
        written = []

        def fake_gh(*args):
            if args[2] == "view":
                return json.dumps({"body": body})
            written.append(Path(args[-1]).read_text())
            return ""

        gh.side_effect = fake_gh
        with tempfile.TemporaryDirectory() as directory:
            swfactory.update_parent_subtask_links("owner/repo", 1, tasks, [7], Path(directory))
            self.assertEqual(gh.call_count, 2)
            self.assertEqual(gh.call_args_list[-1].args[:4],
                             ("owner/repo", "issue", "edit", "1"))
            self.assertEqual(written, [swfactory.parent_body_with_subtask_links(
                body, "owner/repo", tasks, [7])])
            gh.reset_mock()
            body = written[0]
            swfactory.update_parent_subtask_links("owner/repo", 1, tasks, [7], Path(directory))
            gh.assert_called_once_with("owner/repo", "issue", "view", "1", "--json", "body")

    @patch("swfactory.run")
    @patch("swfactory.api_pages")
    def test_registers_and_reuses_linked_subissue(self, pages, run):
        task = {"what": "Add option", "why": "Useful", "how": "Parse then report",
                "tests": "Check exit code"}
        pages.side_effect = [[], []]
        created = {}

        def fake_run(args):
            if "--input" in args:
                payload = json.loads(Path(args[-1]).read_text())
                created.update(payload)
                return json.dumps({"id": 99, "number": 7, "body": payload["body"], "state": "open"})
            return ""

        run.side_effect = fake_run
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(swfactory.register_subtasks("owner/repo", 1, [task], Path(directory)), [7])
        self.assertIn(swfactory.subtask_marker(1, 1), created["body"])
        self.assertEqual(created["labels"], ["swfactory:subtask"])
        self.assertTrue(any("sub_issue_id=99" in call.args[0] for call in run.call_args_list))

        run.reset_mock()
        pages.side_effect = None
        pages.return_value = [{"id": 99, "number": 7, "body": created["body"], "state": "open"}]
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(swfactory.register_subtasks("owner/repo", 1, [task], Path(directory)), [7])
        run.assert_not_called()

    @patch("swfactory.run")
    @patch("swfactory.api_pages")
    def test_unlinked_generated_issue_is_attached_without_duplicate(self, pages, run):
        task = {"what": "Add option", "why": "Useful", "how": "Parse then report",
                "tests": "Check exit code"}
        orphan = {"id": 99, "number": 7, "body": swfactory.subtask_body(1, 1, task),
                  "state": "open"}
        pages.side_effect = [[], [orphan]]
        run.return_value = ""
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(swfactory.register_subtasks("owner/repo", 1, [task], Path(directory)), [7])
        run.assert_called_once_with(["gh", "api", "-X", "POST",
                                     "repos/owner/repo/issues/1/sub_issues", "-F", "sub_issue_id=99"])


class StackedPullRequestTests(unittest.TestCase):
    @patch("swfactory.owner_applied_label", return_value=True)
    @patch("swfactory.gh")
    def test_merge_reconciliation_waits_for_every_pr_and_closes_once(self, gh, _owner):
        parent = issue(1, "accept", "done")
        parent["body"] = (swfactory.SUBTASK_LINKS_START + "\n"
                          "- [#16](https://github.com/owner/repo/issues/16) — first\n"
                          "- [#17](https://github.com/owner/repo/issues/17) — second\n"
                          + swfactory.SUBTASK_LINKS_END)
        states = {16: "OPEN", 17: "OPEN", 1: "OPEN"}
        pr_states = {1: "MERGED", 2: "OPEN"}
        closed = []

        def fake_gh(_repo, *args):
            if args[:2] == ("issue", "list"):
                return json.dumps([parent] if states[1] == "OPEN" else [])
            if args[:2] == ("pr", "list"):
                branch = args[args.index("--head") + 1]
                index = int(branch.rsplit("-", 1)[-1])
                return json.dumps([{"url": "https://github.com/owner/repo/pull/{}".format(18 + index),
                                    "state": pr_states[index], "headRefName": branch,
                                    "title": "Sub-issue #{}: task".format(15 + index)}])
            if args[:2] == ("issue", "view"):
                return json.dumps({"state": states[int(args[2])],
                                   "labels": [{"name": "swfactory:subtask"}]})
            if args[:2] == ("issue", "close"):
                child = int(args[2])
                states[child] = "CLOSED"
                closed.append(child)
                return ""
            raise AssertionError(args)

        gh.side_effect = fake_gh
        swfactory.reconcile_merged_issues("owner/repo", "owner")
        self.assertEqual(closed, [16])
        pr_states[2] = "MERGED"
        swfactory.reconcile_merged_issues("owner/repo", "owner")
        self.assertEqual(closed, [16, 17, 1])
        swfactory.reconcile_merged_issues("owner/repo", "owner")
        self.assertEqual(closed, [16, 17, 1])

    @patch("swfactory.owner_applied_label", return_value=True)
    @patch("swfactory.gh")
    def test_unmerged_or_unmapped_pr_never_closes_parent(self, gh, _owner):
        parent = issue(1, "accept", "done")
        parent["body"] = (swfactory.SUBTASK_LINKS_START + "\n"
                          "- [#16](https://github.com/owner/repo/issues/16)\n"
                          + swfactory.SUBTASK_LINKS_END)
        gh.side_effect = [json.dumps([parent]), json.dumps([])]
        swfactory.reconcile_merged_issues("owner/repo", "owner")
        self.assertEqual(gh.call_count, 2)

    @patch("swfactory.gh")
    def test_existing_task_pr_uses_stable_child_and_base_on_replan(self, gh):
        item = {"url": "https://github.com/owner/repo/pull/20", "state": "OPEN",
                "baseRefName": "main", "title": "Sub-issue #17: Original task wording"}
        gh.return_value = json.dumps([item])
        self.assertEqual(swfactory.existing_task_pr("owner/repo", "swfactory/issue-1-task-2",
                                                     "main", 17), item["url"])
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            swfactory.existing_task_pr("owner/repo", "swfactory/issue-1-task-2", "main", 18)

    @patch("swfactory.update_parent_subtask_links")
    @patch("swfactory.register_subtasks", return_value=[7, 8])
    @patch("swfactory.owner_comments", return_value=[])
    @patch("swfactory.owner_applied_label", return_value=True)
    @patch("swfactory.default_branch", return_value="main")
    @patch("swfactory.gh")
    @patch("swfactory.codex")
    def test_each_subtask_has_own_pr_diff_and_pr_numbered_record(
            self, codex, gh, _branch, _owner, _comments, _subtasks, _links):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            remote = root / "remote.git"
            swfactory.run(["git", "init", "--bare", str(remote)])
            swfactory.run(["git", "init", "-b", "main"], cwd=checkout)
            swfactory.run(["git", "config", "user.name", "Test Owner"], cwd=checkout)
            swfactory.run(["git", "config", "user.email", "test@example.com"], cwd=checkout)
            (checkout / "AGENTS.md").write_text("Record work-items.md and docs/{work number}.md.\n")
            swfactory.run(["git", "add", "AGENTS.md"], cwd=checkout)
            swfactory.run(["git", "commit", "-m", "Base"], cwd=checkout)
            swfactory.run(["git", "push", str(remote), "main"], cwd=checkout)
            work_dir = root / "work"
            work_dir.mkdir()
            created = []
            pr_metadata = {}
            open_children = {7, 8}
            fail_second_task_once = [True]

            def fake_gh(_repo, *args):
                if args[:3] == ("issue", "view", "1"):
                    return json.dumps(issue(1, "accept", "rephased"))
                if args[:2] == ("issue", "view"):
                    return json.dumps({"state": "OPEN" if int(args[2]) in open_children else "CLOSED",
                                       "labels": []})
                if args[:2] == ("issue", "close"):
                    open_children.discard(int(args[2]))
                    return ""
                if args[:2] == ("pr", "list"):
                    return json.dumps([pr_metadata[args[args.index("--head") + 1]]]
                                      if args[args.index("--head") + 1] in pr_metadata else [])
                if args[:2] == ("pr", "create"):
                    base = args[args.index("--base") + 1]
                    head = args[args.index("--head") + 1]
                    created.append((base, head))
                    url = "https://github.com/owner/repo/pull/{}".format(19 + len(created))
                    pr_metadata[head] = {"url": url, "state": "OPEN", "baseRefName": base,
                                         "title": args[args.index("--title") + 1]}
                    return url
                return ""

            def fake_codex(prompt, worktree, output_path=None, schema_path=None, read_only=False):
                if prompt.startswith("Analyze this GitHub issue"):
                    output_path.write_text(json.dumps({
                        "summary": "Two tasks", "why": "Needed", "tasks": [
                            {"what": "First", "why": "Needed", "how": "Add first module",
                             "tests": "Check first"},
                            {"what": "Second", "why": "Needed", "how": "Add second module",
                             "tests": "Check second"}],
                        "integration_tests": ["Run all tests"], "questions": []}))
                elif prompt.startswith("Implement only sub-issue"):
                    index = int(re.search(r"task (\d+) of", prompt).group(1))
                    if index == 2 and fail_second_task_once[0]:
                        fail_second_task_once[0] = False
                        raise RuntimeError("Temporary task failure")
                    (worktree / "feature{}.py".format(index)).write_text("VALUE = {}\n".format(index))
                    (worktree / "test_feature{}.py".format(index)).write_text(
                        "import unittest\nimport feature{}\n\nclass FeatureTests(unittest.TestCase):\n"
                        "    def test_feature_{}(self):\n        self.assertEqual(feature{}.VALUE, {})\n".format(
                            index, index, index, index))
                else:
                    index = int(re.search(r"Review only task (\d+)", prompt).group(1))
                    completed = subprocess.run(["python3", "-m", "unittest", "discover", "-q"],
                                               cwd=worktree, text=True, capture_output=True)
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    output_path.write_text(json.dumps({
                        "passed": True,
                        "test_cases": [{"task": index, "path": "test_feature{}.py".format(index),
                                        "case": "test_feature_{}".format(index),
                                        "behavior": "Module value is correct"}],
                        "test_results": [{"kind": kind, "command": "python3 -m unittest discover -q",
                                          "output": "{} tests passed".format(index), "passed": True}
                                         for kind in ("basic", "functional", "integration")],
                        "details": "All implemented tasks pass"}))

            gh.side_effect = fake_gh
            codex.side_effect = fake_codex
            with self.assertRaisesRegex(RuntimeError, "Temporary task failure"):
                swfactory.process_issue("owner/repo", str(remote), root / "key", checkout,
                                        work_dir, "owner", issue(1, "accept", "rephased"))
            self.assertEqual(len(created), 1)
            swfactory.process_issue("owner/repo", str(remote), root / "key", checkout,
                                    work_dir, "owner", issue(1, "accept", "rephased"))
            self.assertEqual(len(created), 2)
            self.assertEqual(created[0][0], "main")
            self.assertEqual(created[1][0], created[0][1])
            first_files = swfactory.run(["git", "--git-dir", str(remote), "diff", "--name-only",
                                         "refs/heads/main", "refs/heads/{}".format(created[0][1])])
            second_files = swfactory.run(["git", "--git-dir", str(remote), "diff", "--name-only",
                                          "refs/heads/{}".format(created[0][1]),
                                          "refs/heads/{}".format(created[1][1])])
            self.assertIn("docs/20.md", first_files)
            self.assertNotIn("feature2.py", first_files)
            self.assertIn("docs/21.md", second_files)
            self.assertIn("feature2.py", second_files)
            self.assertNotIn("feature1.py", second_files)
            first_run_codex_calls = codex.call_count
            swfactory.process_issue("owner/repo", str(remote), root / "key", checkout,
                                    work_dir, "owner", issue(1, "accept", "rephased"))
            self.assertEqual(len(created), 2)
            self.assertEqual(codex.call_count, first_run_codex_calls + 1)


if __name__ == "__main__":
    unittest.main()
