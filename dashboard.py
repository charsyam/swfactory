"""Local, read-only view of swfactory's structured execution history."""

from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sqlite3
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


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


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


HTML = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>swfactory dashboard</title><link rel="stylesheet" href="/style.css">
<main><header><div><p class="eyebrow">LOCAL RUN HISTORY</p><h1>swfactory</h1>
<p id="repo"></p></div><p id="updated">Loading…</p></header>
<div id="overview">
<section class="cards"><article><h2>Running</h2><strong id="running">0</strong></article>
<article><h2>Active Codex calls</h2><strong id="active-count">0</strong></article>
<article><h2>Last result</h2><strong id="last-result">—</strong></article></section>
<section><h2>Issue workflows</h2><p class="hint">Open an issue to follow its plan, tasks, calls, tests, and PRs.</p><div id="workflows"></div></section>
<section><h2>Active calls</h2><div id="active" class="empty">No active calls</div></section>
<section><h2>Information flow</h2><p class="hint">Structured handoffs and results. Prompts, comments, keys, and full tool output are not stored.</p>
<div id="events" class="event-list"></div></section>
<section><h2>Recent runs</h2><div id="runs"></div></section></div>
<div id="issue-detail" hidden><p><a href="/">← All workflows</a> · <a id="github-issue" href="#">GitHub issue</a></p>
<h2 id="issue-title"></h2><p id="issue-status" class="status"></p><p id="failure-reason" class="status failed" hidden></p>
<section><h2>Stages</h2><div id="stages" class="stage-grid"></div></section>
<section><h2>Agent handoffs</h2><p class="hint">Input references and recorded outputs, connected by call ID.</p><div id="calls"></div></section>
<section><h2>Results and references</h2><div id="artifacts"></div></section>
<section id="performance-section" hidden><h2>Performance experiments</h2><div id="performance"></div></section>
<section><h2>Issue timeline</h2><div id="issue-events"></div><button id="load-older" type="button" hidden>Load older events</button></section>
</div></main>
<script src="/app.js" defer></script><script src="/detail.js" defer></script></html>"""

CSS = """*{box-sizing:border-box}body{margin:0;background:#f4f3ee;color:#182624;font:15px/1.5 system-ui,-apple-system,sans-serif}
main{max-width:1180px;margin:auto;padding:36px 24px 80px}header{display:flex;justify-content:space-between;align-items:end;border-bottom:2px solid #243a35;padding-bottom:20px}
h1{font-size:44px;letter-spacing:-.05em;margin:0}h2{font-size:18px;margin:0 0 14px}.eyebrow{font-size:11px;letter-spacing:.2em;color:#58736a;font-weight:700}
#repo,#updated,.hint,.muted{color:#62736e}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:26px 0}
article,section:not(.cards){background:white;border:1px solid #d9e0d9;border-radius:12px;padding:20px}article strong{font-size:28px}section:not(.cards){margin:18px 0}
.event,.run{display:grid;grid-template-columns:160px 170px 1fr 120px;gap:12px;padding:12px 0;border-top:1px solid #e4e9e4;align-items:start}
.event:first-child,.run:first-child{border-top:0}.flow{font-weight:650}.status{font-size:12px;font-weight:700;text-transform:uppercase;color:#23674e}
.status.failed,.status.interrupted{color:#b04430}.meta{font-size:12px;color:#62736e}.empty{color:#62736e;padding:12px 0}a{color:#14684e}
.workflow,.call,.artifact,.measurement{border-top:1px solid #e4e9e4;padding:14px 0}.workflow:first-child,.call:first-child,.artifact:first-child,.measurement:first-child{border-top:0}
.workflow{display:flex;justify-content:space-between;gap:20px}.workflow a{font-weight:700}.stage-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:10px}
.stage{border:1px solid #d9e0d9;border-radius:9px;padding:12px;background:#f7f9f6}.stage.completed{border-color:#8fbda5}.stage.failed{border-color:#d79b91;background:#fff7f5}.stage.running{border-color:#b8a9e8}
.stage .status{display:block;margin-top:5px}.call strong,.artifact strong{display:block}button{background:#183c32;color:white;border:0;border-radius:7px;padding:9px 14px;cursor:pointer;margin-top:12px}
@media(max-width:700px){.cards{grid-template-columns:1fr}.event,.run{grid-template-columns:1fr 1fr}.event .summary,.run .summary{grid-column:1/-1}header{display:block}}
"""

JS = """const byId=id=>document.getElementById(id);
function node(tag,text,cls){const el=document.createElement(tag);el.textContent=text??'';if(cls)el.className=cls;return el}
function show(container,items,render,empty){container.replaceChildren();if(!items.length){container.append(node('p',empty,'empty'));return}items.forEach(item=>container.append(render(item)))}
function eventRow(e,repo){const row=node('div','','event');row.append(node('time',e.at,'meta'),node('span',e.actor+(e.recipient?' → '+e.recipient:''),'flow'));
const middle=node('div','','summary');middle.append(node('div',e.summary));const meta=node('small','','meta');
if(/^[-A-Za-z0-9_.]+\/[-A-Za-z0-9_.]+$/.test(repo)&&Number.isInteger(e.issue_number)&&e.issue_number>0){const a=node('a','Issue #'+e.issue_number);a.href='https://github.com/'+repo+'/issues/'+e.issue_number;meta.append(a,' · ')}
if(e.task_number)meta.append('Task '+e.task_number+' · ');if(e.agent_id)meta.append('Call '+e.agent_id.slice(0,8)+' · ');meta.append(e.kind);
if(Array.isArray(e.details?.sources))meta.append(' · from '+e.details.sources.join(', '));
const extras=Object.entries(e.details||{}).filter(([key])=>!['sources','url','pr_url'].includes(key)).map(([key,value])=>key+': '+(Array.isArray(value)?value.join(', '):value)).join(' · ');if(extras)meta.append(' · '+extras);
const url=e.details?.url||e.details?.pr_url;if(typeof url==='string'&&/^https:\/\/github\.com\/[-A-Za-z0-9_.]+\/[-A-Za-z0-9_.]+\/pull\/\d+$/.test(url)){const a=node('a','PR #'+url.split('/').pop());a.href=url;meta.append(' · ',a)}
middle.append(meta);row.append(middle,node('span',e.status||'','status '+(e.status||'')));return row}
function runRow(r){const row=node('div','','run');row.append(node('time',r.started_at,'meta'),node('span',r.mode,'flow'),node('span',r.id.slice(0,12),'summary muted'),node('span',r.status,'status '+r.status));return row}
async function refresh(){try{const response=await fetch('/api/state',{cache:'no-store'});if(!response.ok)throw Error('HTTP '+response.status);const data=await response.json();
byId('repo').textContent=data.repo;byId('updated').textContent='Updated '+new Date().toLocaleTimeString();byId('running').textContent=data.runs.filter(r=>r.status==='running').length;
byId('active-count').textContent=data.active_agents.length;byId('last-result').textContent=data.runs[0]?.status||'—';
show(byId('active'),data.active_agents,e=>eventRow(e,data.repo),'No active calls');show(byId('events'),data.events,e=>eventRow(e,data.repo),'No events recorded yet');show(byId('runs'),data.runs,runRow,'No runs recorded yet')
const issues=await (await fetch('/api/issues',{cache:'no-store'})).json();show(byId('workflows'),issues,i=>{const row=node('div','','workflow');const a=node('a','Issue #'+i.issue_number);a.href='/issue/'+i.issue_number;row.append(a,node('span',i.summary+' · '+i.event_count+' events · '+i.at,'meta'),node('span',i.status||i.kind,'status '+(i.status||'')));return row},'No issue history recorded yet');
}catch(error){byId('updated').textContent='Could not load dashboard: '+error.message}}
refresh();setInterval(refresh,5000);
"""

DETAIL_JS = """const issueMatch=location.pathname.match(/^\\/issue\\/([1-9][0-9]*)$/);
if(issueMatch){byId('overview').hidden=true;byId('issue-detail').hidden=false;
const issueNumber=Number(issueMatch[1]);let older=[];let cursor=null;let latest=null;
function renderDetail(data){latest=data;byId('repo').textContent=data.repo;byId('issue-title').textContent='Issue #'+data.issue_number;
byId('issue-status').textContent=data.status;byId('issue-status').className='status '+data.status;
const failure=data.failure;byId('failure-reason').hidden=!failure;byId('failure-reason').textContent=failure?'Failure: '+(failure.reason_code||failure.error_type||'See issue timeline'):'';
byId('github-issue').href='https://github.com/'+data.repo+'/issues/'+data.issue_number;
show(byId('stages'),data.stages,s=>{const el=node('div','','stage '+s.status);el.append(node('strong',s.label),node('span',s.status,'status '+s.status),node('small',s.event_count+' events'+(s.last_at?' · '+s.last_at:''),'meta'));return el},'No stages');
show(byId('calls'),data.calls,c=>{const el=node('div','','call');const duration=c.elapsed_ms==null?'in progress':c.elapsed_ms+' ms';
el.append(node('strong',c.role+' · '+c.status+' · '+duration),node('div','Call '+c.id.slice(0,12)+(c.task_number?' · Task '+c.task_number:''),'meta'));
el.append(node('div','Input: '+(c.sources.length?c.sources.join(', '):'No source categories recorded')));
if(c.references.length)el.append(node('div','References: '+c.references.join(', '),'meta'));
el.append(node('div','Output: '+(c.output||'awaiting result')));const linked=data.artifacts.filter(a=>a.agent_id===c.id);
if(linked.length)el.append(node('div','Result records: '+linked.map(a=>a.kind).join(', '),'meta'));return el},'No agent calls for this issue');
show(byId('artifacts'),data.artifacts,a=>{const el=node('div','','artifact');el.append(node('strong',a.kind.replaceAll('_',' ')),node('small',a.at+(a.task_number?' · Task '+a.task_number:''),'meta'));
for(const [key,value] of Object.entries(a.details)){if((key==='url'||key==='pr_url')&&typeof value==='string'&&/^https:\\/\\/github\\.com\\/[-A-Za-z0-9_.]+\\/[-A-Za-z0-9_.]+\\/pull\\/\\d+$/.test(value)){const link=node('a',key+': '+value);link.href=value;el.append(link)}
else if(key==='issue_numbers'&&Array.isArray(value)){const line=node('div',key+': ','meta');value.filter(Number.isInteger).forEach((number,index)=>{if(index)line.append(', ');const link=node('a','#'+number);link.href='https://github.com/'+data.repo+'/issues/'+number;line.append(link)});el.append(line)}
else el.append(node('div',key+': '+(Array.isArray(value)?value.join(', '):value),'meta'))}return el},'No result references recorded');
byId('performance-section').hidden=!data.performance.length;
show(byId('performance'),data.performance,m=>{const el=node('div','','measurement');const d=m.details;el.append(node('strong',(d.metric||'Metric')+' · attempt '+(d.attempt??'?')));
for(const key of ['target','baseline','candidate','value','unit','status'])if(d[key]!=null)el.append(node('div',key+': '+d[key],'meta'));return el},'No performance experiments recorded');
const combined=[...data.events,...older].filter((item,index,array)=>array.findIndex(other=>other.id===item.id)===index).sort((a,b)=>b.id-a.id);
show(byId('issue-events'),combined,e=>eventRow(e,data.repo),'No issue events');byId('load-older').hidden=!cursor;byId('updated').textContent='Updated '+new Date().toLocaleTimeString()}
async function loadDetail(){try{const response=await fetch('/api/issues/'+issueNumber,{cache:'no-store'});if(!response.ok)throw Error('HTTP '+response.status);const data=await response.json();if(!older.length)cursor=data.next_cursor;renderDetail(data)}catch(error){byId('issue-title').textContent='Could not load issue: '+error.message}}
byId('load-older').addEventListener('click',async()=>{if(!cursor)return;const response=await fetch('/api/issues/'+issueNumber+'?before='+cursor,{cache:'no-store'});if(!response.ok)return;const page=await response.json();older.push(...page.events);cursor=page.next_cursor;renderDetail(latest)});
loadDetail();setInterval(loadDetail,5000)}
"""


def make_server(store, repo, port=8765):
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
                body, mime = JS.encode(), "text/javascript"
            elif path == "/detail.js":
                body, mime = DETAIL_JS.encode(), "text/javascript"
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
