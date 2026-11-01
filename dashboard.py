"""Local, read-only view of swfactory's structured execution history."""

from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit
from uuid import uuid4


SCHEMA_VERSION = 1


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
<section class="cards"><article><h2>Running</h2><strong id="running">0</strong></article>
<article><h2>Active Codex calls</h2><strong id="active-count">0</strong></article>
<article><h2>Last result</h2><strong id="last-result">—</strong></article></section>
<section><h2>Active calls</h2><div id="active" class="empty">No active calls</div></section>
<section><h2>Information flow</h2><p class="hint">Structured handoffs and results. Prompts, comments, keys, and full tool output are not stored.</p>
<div id="events" class="event-list"></div></section>
<section><h2>Recent runs</h2><div id="runs"></div></section></main>
<script src="/app.js" defer></script></html>"""

CSS = """*{box-sizing:border-box}body{margin:0;background:#f4f3ee;color:#182624;font:15px/1.5 system-ui,-apple-system,sans-serif}
main{max-width:1180px;margin:auto;padding:36px 24px 80px}header{display:flex;justify-content:space-between;align-items:end;border-bottom:2px solid #243a35;padding-bottom:20px}
h1{font-size:44px;letter-spacing:-.05em;margin:0}h2{font-size:18px;margin:0 0 14px}.eyebrow{font-size:11px;letter-spacing:.2em;color:#58736a;font-weight:700}
#repo,#updated,.hint,.muted{color:#62736e}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:26px 0}
article,section:not(.cards){background:white;border:1px solid #d9e0d9;border-radius:12px;padding:20px}article strong{font-size:28px}section:not(.cards){margin:18px 0}
.event,.run{display:grid;grid-template-columns:160px 170px 1fr 120px;gap:12px;padding:12px 0;border-top:1px solid #e4e9e4;align-items:start}
.event:first-child,.run:first-child{border-top:0}.flow{font-weight:650}.status{font-size:12px;font-weight:700;text-transform:uppercase;color:#23674e}
.status.failed,.status.interrupted{color:#b04430}.meta{font-size:12px;color:#62736e}.empty{color:#62736e;padding:12px 0}a{color:#14684e}
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
}catch(error){byId('updated').textContent='Could not load dashboard: '+error.message}}
refresh();setInterval(refresh,5000);
"""


def make_server(store, repo, port=8765):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            host = self.headers.get("Host", "").split(":", 1)[0]
            if host not in ("127.0.0.1", "localhost"):
                self.send_error(400, "Local host required")
                return
            path = urlsplit(self.path).path
            if path == "/api/state":
                body, mime = json.dumps(store.snapshot(repo), ensure_ascii=False).encode(), "application/json"
            elif path == "/":
                body, mime = HTML.encode(), "text/html"
            elif path == "/style.css":
                body, mime = CSS.encode(), "text/css"
            elif path == "/app.js":
                body, mime = JS.encode(), "text/javascript"
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
