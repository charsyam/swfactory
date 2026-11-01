"""Local, read-only view of swfactory's structured execution history."""

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from threading import Lock
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4


SCHEMA_VERSION = 1
STAGES = (
    ("specification", "Specification"),
    ("plan", "Plan"),
    ("subtasks", "Subtasks"),
    ("implementation", "Implementation"),
    ("performance", "Experiments"),
    ("verification", "Tests"),
    ("review", "Pull requests"),
    ("merge", "Merge"),
)
STAGE_FOR_KIND = {
    "issue_rephrased": "specification", "plan_ready": "plan",
    "continuity_checked": "plan", "implementation_result": "implementation",
    "subtasks_registered": "subtasks", "tests_verified": "verification",
    "performance_measurement": "performance",
    "pr_created": "review", "pr_reused": "review", "issue_ready": "review",
    "subtask_closed": "merge", "issue_closed": "merge",
}
ARTIFACT_KINDS = {"plan_ready", "continuity_checked", "subtasks_registered",
                  "implementation_result", "tests_verified",
                  "pr_created", "pr_reused", "subtask_closed", "issue_closed"}


class GitHubSnapshot:
    """Short-lived, read-only GitHub cache; never stores issue bodies or comments."""

    def __init__(self, repo, config_dir, ttl=60):
        self.repo, self.config_dir, self.ttl = repo, str(config_dir), ttl
        self.lock = Lock()
        self.next_refresh = 0
        self.data = {"repo": repo, "synced_at": None, "error": None,
                     "issues": [], "pull_requests": []}

    def _list(self, noun, fields):
        env = os.environ.copy()
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
        env["GH_CONFIG_DIR"] = self.config_dir
        completed = subprocess.run(
            ["gh", noun, "list", "--repo", self.repo, "--state", "all",
             "--limit", "1000", "--json", fields], capture_output=True, text=True,
            timeout=20, env=env, check=False)
        if completed.returncode:
            raise RuntimeError("github_query_failed")
        return json.loads(completed.stdout)

    def _subissues(self, number):
        env = os.environ.copy()
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
        env["GH_CONFIG_DIR"] = self.config_dir
        completed = subprocess.run(
            ["gh", "api", "repos/{}/issues/{}/sub_issues".format(self.repo, number),
             "--paginate", "--jq", ".[].number"], capture_output=True, text=True,
            timeout=20, env=env, check=False)
        if completed.returncode:
            raise RuntimeError("github_subissues_failed")
        return [int(value) for value in completed.stdout.splitlines() if value.isdecimal()]

    def snapshot(self):
        with self.lock:
            if time.monotonic() < self.next_refresh:
                return self.data
            self.next_refresh = time.monotonic() + self.ttl
            try:
                issues = self._list("issue", "number,title,state,labels,updatedAt,url")
                pulls = self._list("pr", "number,title,state,isDraft,mergedAt,updatedAt,url,closingIssuesReferences")
                issue_numbers = {item["number"] for item in issues}
                for issue in issues:
                    labels = {label["name"] for label in issue.pop("labels", [])}
                    issue["labels"] = sorted(labels)
                    issue["parent"] = None
                    match = re.match(r"^\[#([1-9][0-9]*) task [1-9][0-9]*\]", issue["title"])
                    if "swfactory:subtask" in labels and match:
                        issue["parent"] = int(match.group(1))
                    issue.pop("body", None)
                    issue["pull_requests"] = []
                    issue["children"] = []
                by_number = {item["number"]: item for item in issues}
                candidates = [item["number"] for item in issues if not item["parent"]
                              and "swfactory:subtask" not in item["labels"]
                              and set(item["labels"]) & {"accept", "done", "rephased", "swfactory:waiting"}]
                with ThreadPoolExecutor(max_workers=4) as pool:
                    native = list(pool.map(self._subissues, candidates))
                for parent, children in zip(candidates, native):
                    for child in children:
                        if child in by_number and "swfactory:subtask" in by_number[child]["labels"]:
                            by_number[child]["parent"] = parent
                for issue in issues:
                    if issue["parent"] in issue_numbers:
                        by_number[issue["parent"]]["children"].append(issue["number"])
                for issue in issues:
                    issue["children"].sort()
                for pull in pulls:
                    pull["issues"] = sorted({item["number"] for item in
                                             pull.pop("closingIssuesReferences", [])
                                             if item.get("number") in issue_numbers})
                    for number in pull["issues"]:
                        by_number[number]["pull_requests"].append(pull["number"])
                self.data = {"repo": self.repo, "synced_at": now(), "error": None,
                             "issues": issues, "pull_requests": pulls}
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
                self.data = {**self.data, "error": type(error).__name__}
            return self.data


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def build_board(github, telemetry):
    """Turn raw sources into decisions and delivery paths for the owner."""
    issues = {issue["number"]: issue for issue in github["issues"]}
    pulls = {pull["number"]: pull for pull in github["pull_requests"]}
    observed = {item["issue_number"]: item for item in telemetry["issues"]}
    groups = {key: [] for key in (
        "answer", "recover", "review", "approve", "running", "queued", "completed")}
    for issue in github["issues"]:
        if issue["parent"]:
            continue
        children = [issues[number] for number in issue["children"] if number in issues]
        numbers = sorted(set(issue["pull_requests"]).union(
            number for child in children for number in child["pull_requests"]))
        linked = [pulls[number] for number in numbers if number in pulls]
        open_prs = [pull["number"] for pull in linked if pull["state"] == "OPEN"]
        merged_prs = [pull["number"] for pull in linked if pull["mergedAt"]]
        labels = set(issue["labels"])
        if issue["state"] == "CLOSED":
            group, next_step = "completed", "Closed on GitHub"
        elif "swfactory:waiting" in labels:
            group, next_step = "answer", "Answer the owner question on the issue"
        elif "swfactory:failed" in labels:
            group, next_step = "recover", "Inspect the failure; remove the failed label after fixing it"
        elif open_prs or "done" in labels:
            group = "review"
            next_step = ("Review and merge the open PRs" if open_prs else
                         "Verify the implementation PR link and merge state")
        elif "swfactory:running" in labels:
            group, next_step = "running", "Automation is processing this issue"
        elif "accept" in labels:
            group, next_step = "queued", "Await the next controller run"
        else:
            group, next_step = "approve", "Review the specification; add accept to authorize work"
        groups[group].append({
            "number": issue["number"], "title": issue["title"], "updated_at": issue["updatedAt"],
            "labels": issue["labels"], "group": group, "next_step": next_step,
            "children": [{"number": child["number"], "title": child["title"],
                          "state": child["state"], "pull_requests": child["pull_requests"]}
                         for child in children],
            "child_closed": sum(child["state"] == "CLOSED" for child in children),
            "pull_requests": [{"number": pull["number"], "title": pull["title"],
                               "state": pull["state"], "merged_at": pull["mergedAt"],
                               "issues": pull["issues"]} for pull in linked],
            "open_prs": open_prs, "merged_prs": merged_prs,
            "local_status": observed.get(issue["number"], {}).get("status"),
            "local_events": observed.get(issue["number"], {}).get("event_count", 0),
        })
    for group in groups.values():
        group.sort(key=lambda item: item["updated_at"], reverse=True)
    unlinked = [{"number": pull["number"], "title": pull["title"],
                 "updated_at": pull["updatedAt"]} for pull in github["pull_requests"]
                if pull["state"] == "OPEN" and not pull["issues"]]
    return {"repo": github["repo"], "synced_at": github["synced_at"],
            "github_error": github["error"], "groups": groups,
            "unlinked_prs": unlinked, "active_calls": telemetry["active_agents"],
            "latest_run": telemetry["runs"][0] if telemetry["runs"] else None,
            "history_count": len(telemetry["issues"])}


class EventStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY, version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, repo TEXT NOT NULL, mode TEXT NOT NULL,
                    started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL, repo TEXT NOT NULL, at TEXT NOT NULL,
                    kind TEXT NOT NULL, actor TEXT NOT NULL, recipient TEXT,
                    issue_number INTEGER, task_number INTEGER, agent_id TEXT,
                    status TEXT, summary TEXT NOT NULL, details TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );
                CREATE INDEX IF NOT EXISTS events_repo_id ON events(repo, id DESC);
                CREATE INDEX IF NOT EXISTS events_agent ON events(agent_id, id);
            """)
            db.execute("INSERT OR IGNORE INTO schema_meta(key, version) VALUES ('schema', ?)",
                       (SCHEMA_VERSION,))
            version = db.execute("SELECT version FROM schema_meta WHERE key='schema'").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise RuntimeError("Unsupported telemetry schema version")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=5)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA foreign_keys=ON")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def start_run(self, repo, mode):
        run_id = uuid4().hex
        timestamp = now()
        with self.connect() as db:
            # The controller holds the per-repository lock, so a former running
            # row belongs to a process that ended without recording completion.
            db.execute("UPDATE runs SET status='interrupted', finished_at=? "
                       "WHERE repo=? AND status='running'", (timestamp, repo))
            db.execute("INSERT INTO runs VALUES (?, ?, ?, ?, NULL, 'running')",
                       (run_id, repo, mode, timestamp))
            self._insert(db, run_id, repo, "run_started", "controller", None,
                         None, None, None, "running", "Controller run started", {})
        return run_id

    def finish_run(self, run_id, repo, status):
        if status not in ("completed", "failed"):
            raise ValueError("Invalid run status")
        with self.connect() as db:
            db.execute("UPDATE runs SET status=?, finished_at=? WHERE id=? AND repo=?",
                       (status, now(), run_id, repo))
            self._insert(db, run_id, repo, "run_finished", "controller", None,
                         None, None, None, status, "Controller run finished", {})

    def record(self, run_id, repo, kind, actor, recipient=None, issue_number=None,
               task_number=None, agent_id=None, status=None, summary="", details=None):
        with self.connect() as db:
            self._insert(db, run_id, repo, kind, actor, recipient, issue_number,
                         task_number, agent_id, status, summary, details or {})

    @staticmethod
    def _insert(db, run_id, repo, kind, actor, recipient, issue_number,
                task_number, agent_id, status, summary, details):
        db.execute("INSERT INTO events(run_id, repo, at, kind, actor, recipient, "
                   "issue_number, task_number, agent_id, status, summary, details) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (run_id, repo, now(), kind, actor, recipient, issue_number,
                    task_number, agent_id, status, summary[:240],
                    json.dumps(details, ensure_ascii=False, sort_keys=True)))

    def snapshot(self, repo):
        with self.connect() as db:
            runs = [dict(row) for row in db.execute(
                "SELECT * FROM runs WHERE repo=? ORDER BY started_at DESC, rowid DESC LIMIT 20",
                (repo,))]
            events = [dict(row) for row in db.execute(
                "SELECT * FROM events WHERE repo=? ORDER BY id DESC LIMIT 200", (repo,))]
            active = [dict(row) for row in db.execute("""
                SELECT e.* FROM events e JOIN runs r ON r.id=e.run_id
                WHERE e.repo=? AND e.kind='agent_started' AND r.status='running'
                  AND NOT EXISTS (SELECT 1 FROM events done
                                  WHERE done.agent_id=e.agent_id AND done.kind='agent_finished')
                ORDER BY e.id DESC LIMIT 100
            """, (repo,))]
        for event in events:
            event["details"] = json.loads(event["details"])
        for event in active:
            event["details"] = json.loads(event["details"])
        return {"repo": repo, "runs": runs, "events": events, "active_agents": active}

    def issue_list(self, repo, limit=100):
        with self.connect() as db:
            rows = db.execute("""
                SELECT e.issue_number, e.at, e.kind, e.status, e.summary,
                       counts.event_count
                FROM events e JOIN (
                    SELECT issue_number, MAX(id) AS last_id, COUNT(*) AS event_count
                    FROM events WHERE repo=? AND issue_number IS NOT NULL
                    GROUP BY issue_number
                ) counts ON e.id=counts.last_id
                ORDER BY e.id DESC LIMIT ?
            """, (repo, limit)).fetchall()
            state_rows = db.execute("""
                SELECT issue_number, kind FROM events
                WHERE repo=? AND kind IN ('issue_started', 'issue_failed', 'issue_ready',
                                          'issue_closed', 'owner_question', 'owner_answer_seen')
                ORDER BY id DESC
            """, (repo,)).fetchall()
        states = {}
        status_for_kind = {"issue_started": "running", "issue_failed": "failed",
                           "issue_ready": "ready", "issue_closed": "merged",
                           "owner_question": "waiting", "owner_answer_seen": "running"}
        for row in state_rows:
            states.setdefault(row["issue_number"], status_for_kind[row["kind"]])
        result = [dict(row) for row in rows]
        for row in result:
            row["status"] = states.get(row["issue_number"], "observed")
        return result

    def issue_snapshot(self, repo, issue_number, before=None, page_size=100):
        if type(issue_number) is not int or issue_number < 1:
            raise ValueError("Issue number must be positive")
        if before is not None and (type(before) is not int or before < 1):
            raise ValueError("Invalid event cursor")
        with self.connect() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM events WHERE repo=? AND issue_number=? ORDER BY id ASC",
                (repo, issue_number))]
        if not rows:
            return None
        for row in rows:
            row["details"] = json.loads(row["details"])
        stages = {key: {"key": key, "label": label, "status": "pending",
                        "last_at": None, "event_count": 0} for key, label in STAGES}
        calls = {}
        artifacts = []
        performance = []
        current_stage = "specification"
        issue_status = "observed"
        failure = None
        for row in rows:
            kind, status = row["kind"], row["status"]
            role = row["recipient"] if kind == "agent_started" else row["actor"]
            stage = STAGE_FOR_KIND.get(kind)
            if role in ("rephraser", "planner", "continuity", "implementer", "verifier") and kind.startswith("agent_"):
                stage = {"rephraser": "specification", "planner": "plan",
                         "continuity": "plan", "implementer": "implementation",
                         "verifier": "verification"}[role]
            if kind == "owner_question":
                stage = current_stage
                issue_status = "waiting"
            elif kind == "owner_answer_seen":
                issue_status = "running"
            elif kind == "issue_started":
                issue_status = "running"
                failure = None
            elif kind == "issue_failed":
                stage = current_stage
                issue_status = "failed"
                failure = {"at": row["at"], "reason_code": row["details"].get("reason_code"),
                           "error_type": row["details"].get("error_type"),
                           "issue_url": row["details"].get("issue_url")}
            elif kind == "issue_ready":
                issue_status = "ready"
            elif kind == "issue_closed":
                issue_status = "merged"
            if stage:
                current_stage = stage
                item = stages[stage]
                item["event_count"] += 1
                item["last_at"] = row["at"]
                item["status"] = ("waiting" if kind == "owner_question" else
                                  "failed" if kind == "issue_failed" else
                                  "running" if kind == "agent_started" else
                                  "completed" if status == "completed" else status or "observed")
            if kind == "agent_started" and row["agent_id"]:
                calls[row["agent_id"]] = {"id": row["agent_id"], "role": role,
                                          "task_number": row["task_number"],
                                          "started_at": row["at"], "status": "running",
                                          "sources": row["details"].get("sources", []),
                                          "references": row["details"].get("references", []),
                                          "elapsed_ms": None, "output": None}
            elif kind == "agent_finished" and row["agent_id"] in calls:
                call = calls[row["agent_id"]]
                call.update(status=status, elapsed_ms=row["details"].get("elapsed_ms"),
                            output=row["details"].get("result") or row["details"].get("error_type"))
            if kind in ARTIFACT_KINDS:
                artifacts.append({"kind": kind, "at": row["at"], "task_number": row["task_number"],
                                  "agent_id": row["agent_id"], "details": row["details"]})
            if kind == "performance_measurement":
                performance.append({"at": row["at"], "task_number": row["task_number"],
                                    "details": row["details"]})
        page = [row for row in rows if before is None or row["id"] < before]
        page = page[-(page_size + 1):]
        has_more = len(page) > page_size
        if has_more:
            page = page[1:]
        page.reverse()
        return {"repo": repo, "issue_number": issue_number, "status": issue_status,
                "failure": failure,
                "stages": list(stages.values()), "calls": list(reversed(list(calls.values()))),
                "artifacts": list(reversed(artifacts[-100:])),
                "performance": list(reversed(performance[-100:])),
                "events": page, "next_cursor": page[-1]["id"] if has_more else None}


class RunObserver:
    def __init__(self, store, repo, run_id):
        self.store, self.repo, self.run_id = store, repo, run_id

    def record(self, kind, actor="controller", recipient=None, issue_number=None,
               task_number=None, agent_id=None, status=None, summary="", details=None):
        self.store.record(self.run_id, self.repo, kind, actor, recipient,
                          issue_number, task_number, agent_id, status, summary, details)


HTML = """<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>swfactory 작업 현황</title><link rel="stylesheet" href="/style.css">
<main><header><div><p class="eyebrow">SWFACTORY · OWNER VIEW</p><h1>작업 현황</h1>
<p id="repo"></p></div><p id="updated">불러오는 중…</p></header>
<div id="overview">
<div class="summary-bar"><strong id="decision-count">—</strong><span>owner 결정 필요</span><strong id="automation-count">—</strong><span>자동화 대기·진행</span><span id="source-note" class="source-note"></span></div>
<section class="priority"><div class="section-head"><div><p class="eyebrow">01 / OWNER</p><h2>지금 결정할 일</h2></div><p>답변, 실패 복구, PR 검토, 승인 순서로 표시합니다.</p></div><div id="decisions"></div></section>
<section><div class="section-head"><div><p class="eyebrow">02 / AUTOMATION</p><h2>자동화 진행과 대기</h2></div><p id="run-status"></p></div><div id="active"></div><div id="automation"></div></section>
<section id="unlinked-section" hidden><div class="section-head"><div><p class="eyebrow">CHECK / LINKAGE</p><h2>이슈 연결이 없는 열린 PR</h2></div></div><div id="unlinked"></div></section>
<details class="archive"><summary>완료된 작업 보기 <span id="completed-count"></span></summary><div id="completed"></div></details>
<section id="history-section" hidden><div class="section-head"><div><p class="eyebrow">LOCAL / TRACE</p><h2>최근 에이전트 전달</h2></div><p>이 서버에서 기록된 실행만 표시합니다.</p></div><div id="events"></div></section></div>
<div id="issue-detail" hidden><nav><a href="/">← 작업 현황</a><a id="github-issue" href="#">GitHub에서 열기 ↗</a></nav>
<p class="eyebrow">ISSUE / DELIVERY PATH</p><h2 id="issue-title"></h2><p id="issue-action" class="action"></p>
<div id="issue-facts" class="fact-strip"></div>
<section><div class="section-head"><div><p class="eyebrow">01 / DELIVERY</p><h2>하위 작업과 PR</h2></div><p>이슈에서 구현·병합까지의 연결</p></div><div id="delivery"></div></section>
<section id="verification-section"><div class="section-head"><div><p class="eyebrow">02 / EVIDENCE</p><h2>테스트와 결과 근거</h2></div></div><div id="verification"></div></section>
<section id="agent-section" hidden><div class="section-head"><div><p class="eyebrow">03 / AGENTS</p><h2>에이전트 전달</h2></div></div><div id="calls"></div></section>
<section id="performance-section" hidden><h2>성능 실험</h2><div id="performance"></div></section>
<details id="trace-section" class="archive" hidden><summary>실행 이벤트 원문 보기</summary><div id="issue-events"></div><button id="load-older" type="button" hidden>이전 이벤트 더 보기</button></details>
</div></main>
<script src="/app.js" defer></script><script src="/detail.js" defer></script></html>"""

CSS = """*{box-sizing:border-box}body{margin:0;background:#f5f6f3;color:#1a2924;font:15px/1.55 system-ui,-apple-system,sans-serif}
main{max-width:1160px;margin:auto;padding:34px 24px 80px}header{display:flex;align-items:end;justify-content:space-between;border-bottom:1px solid #cdd9d0;padding-bottom:25px;gap:20px}
h1{font-size:42px;letter-spacing:-.05em;margin:0}h2{font-size:22px;letter-spacing:-.03em;margin:0}.eyebrow{font-size:11px;letter-spacing:.16em;font-weight:800;color:#547363;margin:0 0 6px}
#repo,#updated,.meta,.section-head p,.source-note{color:#60736a}#repo,#updated{margin:3px 0}.summary-bar{display:flex;align-items:baseline;gap:9px;padding:22px 2px 8px;flex-wrap:wrap}.summary-bar strong{font-size:25px;color:#123e2b}.summary-bar span:not(.source-note){font-size:13px;margin-right:24px}.source-note{margin-left:auto;font-size:12px}
section,.archive{background:#fff;border:1px solid #d9e3db;border-radius:13px;padding:23px;margin:18px 0}.priority{border-top:4px solid #2e7553}.section-head{display:flex;justify-content:space-between;align-items:end;gap:18px;margin-bottom:14px}.section-head p:last-child{font-size:12px;margin:0}.group{border-top:1px solid #e5ebe5;padding-top:16px;margin-top:16px}.group:first-child{border-top:0;margin-top:0;padding-top:0}.group-head{display:flex;align-items:baseline;gap:10px;margin-bottom:4px}.group-head h3{font-size:15px;margin:0}.group-head small{color:#6d7e73}.group.recover h3{color:#a34432}.group.review h3{color:#28644d}
.work-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(170px,260px);gap:18px;padding:13px 0;border-top:1px solid #edf0ea}.work-row:first-of-type{border-top:0}.work-row a{font-weight:700}.work-row .title{display:block;color:#21362c;margin:3px 0}.work-row .meta{display:block;font-size:12px}.next{font-size:13px;color:#31523e;background:#eef4ef;border-radius:7px;padding:9px 11px}.next b{display:block;color:#5a7063;font-size:11px;margin-bottom:2px}.pill{display:inline-block;background:#e9f1e9;color:#285b40;border-radius:100px;padding:2px 8px;font-size:11px;font-weight:700}.pill.failed{background:#fff0ea;color:#a34432}.pill.review{background:#eaf2ff;color:#315c9f}.muted{color:#718078}.empty{color:#718078;padding:10px 0}a{color:#176443;text-decoration:none}a:hover{text-decoration:underline}
.flow-row{display:grid;grid-template-columns:55px minmax(0,1fr) 160px;gap:12px;border-top:1px solid #edf0ea;padding:11px 0;align-items:start}.flow-row:first-child{border-top:0}.flow-row .meta{font-size:12px}.flow-row .status{text-align:right}.status{font-size:12px;font-weight:700;color:#2b6a4d}.status.failed{color:#ab4a33}.fact-strip{display:flex;flex-wrap:wrap;gap:9px;margin:12px 0 21px}.fact{border:1px solid #dce6dd;background:#fff;border-radius:7px;padding:7px 10px;font-size:12px}.action{font-size:17px;font-weight:700;color:#214c35;background:#e8f3e9;border-left:4px solid #2e7553;padding:13px 16px;border-radius:5px}.archive summary{cursor:pointer;font-weight:700}.archive[hidden],section[hidden]{display:none}nav{display:flex;justify-content:space-between;margin:10px 0 30px}.evidence,.call,.event,.measurement{padding:12px 0;border-top:1px solid #edf0ea}.evidence:first-child,.call:first-child,.event:first-child,.measurement:first-child{border-top:0}.event{display:grid;grid-template-columns:170px 150px 1fr;gap:10px;font-size:13px}.event .meta{font-size:11px}.call strong{display:block}.call small{display:block;color:#718078}button{background:#214c35;color:white;border:0;border-radius:7px;padding:9px 14px;cursor:pointer;margin-top:14px}
@media(max-width:720px){main{padding:22px 14px 60px}header,.section-head{display:block}h1{font-size:34px}.source-note{width:100%;margin-left:0}.work-row{grid-template-columns:1fr}.flow-row{grid-template-columns:42px 1fr}.flow-row .status{grid-column:2;text-align:left}.event{grid-template-columns:1fr 1fr}.event .summary{grid-column:1/-1}}
"""

ASSETS = Path(__file__).resolve().parent / "dashboard_assets"

def make_server(store, repo, port=8765, github=None):
    app_js = (ASSETS / "app.js").read_bytes()
    detail_js = (ASSETS / "detail.js").read_bytes()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            host = self.headers.get("Host", "").split(":", 1)[0]
            if host not in ("127.0.0.1", "localhost"):
                self.send_error(400, "Local host required")
                return
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/api/state":
                body, mime = json.dumps(store.snapshot(repo), ensure_ascii=False).encode(), "application/json"
            elif path == "/api/board":
                data = github.snapshot() if github else {"repo": repo, "synced_at": None,
                                                         "error": "not_configured", "issues": [],
                                                         "pull_requests": []}
                history = store.snapshot(repo)
                history["issues"] = store.issue_list(repo)
                body, mime = json.dumps(build_board(data, history), ensure_ascii=False).encode(), "application/json"
            elif path == "/api/github":
                data = github.snapshot() if github else {"repo": repo, "synced_at": None,
                                                         "error": "not_configured", "issues": [],
                                                         "pull_requests": []}
                body, mime = json.dumps(data, ensure_ascii=False).encode(), "application/json"
            elif path == "/api/issues":
                body, mime = json.dumps(store.issue_list(repo), ensure_ascii=False).encode(), "application/json"
            elif re.fullmatch(r"/api/issues/[1-9][0-9]*", path):
                values = parse_qs(parsed.query).get("before", [])
                if len(values) > 1 or (values and not re.fullmatch(r"[1-9][0-9]*", values[0])):
                    self.send_error(400, "Invalid event cursor")
                    return
                result = store.issue_snapshot(repo, int(path.rsplit("/", 1)[-1]),
                                              int(values[0]) if values else None)
                if result is None:
                    self.send_error(404, "No local history for issue")
                    return
                body, mime = json.dumps(result, ensure_ascii=False).encode(), "application/json"
            elif path == "/" or re.fullmatch(r"/issue/[1-9][0-9]*", path):
                body, mime = HTML.encode(), "text/html"
            elif path == "/style.css":
                body, mime = CSS.encode(), "text/css"
            elif path == "/app.js":
                body, mime = app_js, "text/javascript"
            elif path == "/detail.js":
                body, mime = detail_js, "text/javascript"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.send_error(405)

        def log_message(self, _format, *_args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
