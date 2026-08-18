#!/usr/bin/env python3
"""
Cursor Daemon v2 — Named operations, web-mapped commands, zero-token relay.
"""
import asyncio, json, os, subprocess, html, shutil, tempfile, re, shlex
from pathlib import Path
from datetime import datetime, timezone

EDGE_VOICE = "en-US-AriaNeural"
TTS_MAX_CHARS = 3000

import asyncio as _asyncio
_state_lock = _asyncio.Lock()

# Config
PORT = 9000
HOST = "0.0.0.0"
STATE_FILE = Path("/home/ubuntu/.cursor-daemon/state.json")
WORK_DIR = Path("/home/ubuntu/work")
CLI = Path("/home/ubuntu/.local/bin/agent")

# Default command mappings
DM = {}
for i in range(1, 14):
    DM[str(i)] = "select_project_%d" % i
    DM["switch to %d" % i] = "select_project_%d" % i
    DM["go to %d" % i] = "select_project_%d" % i
    DM["select %d" % i] = "select_project_%d" % i
    DM["project %d" % i] = "select_project_%d" % i
DM.update({
    "projects": "display_projects", "back": "display_projects",
    "go back": "display_projects", "menu": "display_projects",
    "show projects": "display_projects", "list projects": "display_projects",
    "status": "display_status", "show status": "display_status",
    "current project": "display_status", "what project": "display_status",
    "help": "display_help", "show help": "display_help",
    "lock": "display_lock", "show lock": "display_lock",
    "force": "force_lock", "force lock": "force_lock",
    "plan on": "mode_plan_on", "plan off": "mode_plan_off",
    "turn on plan": "mode_plan_on", "turn off plan": "mode_plan_off",
    "enable plan": "mode_plan_on", "disable plan": "mode_plan_off",
    "mode agent": "mode_agent", "mode plan": "mode_plan",
    "mode ask": "mode_ask", "mode shell": "mode_shell",
    "agent mode": "mode_agent", "plan mode": "mode_plan",
    "ask mode": "mode_ask", "shell mode": "mode_shell",
    "new context": "new_context", "newcontext": "new_context",
    "fresh context": "new_context", "fresh": "new_context",
})

OPS = [
    ("display_projects", "List projects"),
    ("cursor_prompt", "Pass to Cursor Agent"),
    ("display_status", "Show status"),
    ("display_help", "Show help"),
    ("display_lock", "Show lock"),
    ("force_lock", "Force lock override"),
    ("new_context", "Start new Cursor context"),
    ("mode_agent", "Mode: agent"),
    ("mode_plan", "Mode: plan"),
    ("mode_ask", "Mode: ask"),
    ("mode_shell", "Mode: shell"),
    ("mode_plan_on", "Plan on (shortcut)"),
    ("mode_plan_off", "Plan off (shortcut)"),
] + [("select_project_%d" % i, "Select project %d" % i) for i in range(1, 14)]

# State
def load_state():
    if STATE_FILE.exists():
        s = json.loads(STATE_FILE.read_text())
    else:
        s = {}
    s.setdefault("bots", {})
    s.setdefault("logs", [])
    mappings = s.setdefault("mappings", dict(DM))
    for k, v in DM.items():
        mappings.setdefault(k, v)
    return s

def save_state(s):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(s, indent=2, default=str))

def log(s, msg, level="info"):
    e = {"time": datetime.now(timezone.utc).isoformat(), "msg": msg, "level": level}
    s.setdefault("logs", []).append(e)
    if len(s["logs"]) > 200:
        s["logs"] = s["logs"][-200:]
    print("[%s] [%s] %s" % (e["time"], level, msg))

def bot_state(s, token):
    b = s["bots"].setdefault(token, {})
    b.setdefault("project", "")
    b.setdefault("current_mode", "agent")
    b.setdefault("current_model", "")
    b.setdefault("last_command", "")
    b.setdefault("voice_enabled", False)
    b.setdefault("yolo_enabled", False)
    b.setdefault("continue_enabled", True)
    return b

# Lock helpers
def _read_owner(lockd):
    owner_file = lockd / "owner"
    if not owner_file.exists():
        return None
    for line in owner_file.read_text().splitlines():
        line = line.strip()
        if line.startswith("agent_id="):
            return line.split("=", 1)[1].strip()
    return None

def _write_lock(lockd, bn):
    lockd.mkdir(exist_ok=True)
    (lockd / "owner").write_text(
        "agent_id=%s\nlocked_at=%s" % (bn, datetime.now(timezone.utc).isoformat())
    )

def _release_lock_if_ours(proj, bn):
    if not proj:
        return
    lockd = WORK_DIR / proj / ".agent-lock"
    if _read_owner(lockd) == bn:
        shutil.rmtree(lockd, ignore_errors=True)

def _move_project(bs, name):
    """Clear source lock, then lock destination. Refuse if dest held by other."""
    bn = bs.get("name", "daemon")
    old_proj = bs.get("project", "")
    if old_proj == name:
        lockd = WORK_DIR / name / ".agent-lock"
        owner = _read_owner(lockd)
        if owner and owner != bn:
            return "LOCKED by %s. Use 'force' to override." % owner
        _write_lock(lockd, bn)
        return "Now in: %s\n\nUse 'cursor <prompt>' to talk to Cursor Agent. Type 'projects' to switch." % name

    dest_lock = WORK_DIR / name / ".agent-lock"
    dest_owner = _read_owner(dest_lock)
    if dest_owner and dest_owner != bn:
        return "LOCKED by %s. Use 'force' to override." % dest_owner

    _release_lock_if_ours(old_proj, bn)
    _write_lock(dest_lock, bn)
    bs["project"] = name
    bs["continue_enabled"] = False
    bs["current_mode"] = "agent"
    return (
        "Now in: %s\nContext: NEW (next cursor starts fresh)\n\n"
        "Use 'cursor <prompt>' to talk to Cursor Agent. Type 'projects' to switch."
    ) % name

# Operations
def _projects():
    return sorted([
        d.name for d in WORK_DIR.iterdir()
        if d.is_dir() and not d.name.startswith(".") and d.name != "cursor-daemon"
    ])

def _projects_list():
    projs = _projects()
    if not projs:
        return "No projects found."
    lines = ["Select a project:", ""]
    for i, p in enumerate(projs, 1):
        lines.append("%d. %s" % (i, p))
    lines.append("")
    lines.append("Reply with a number (1-%d) to select." % len(projs))
    return "\n".join(lines)

def _select(bs, num):
    projs = _projects()
    if num < 1 or num > len(projs):
        return "Invalid. Pick 1-%d." % len(projs)
    return _move_project(bs, projs[num - 1])

def _status(bs):
    p = bs.get("project", "none")
    m = bs.get("current_mode", "agent")
    mdl = bs.get("current_model", "(default)")
    voice = "ON" if bs.get("voice_enabled") else "OFF"
    yolo = "ON" if bs.get("yolo_enabled") else "OFF"
    cont = "ON" if bs.get("continue_enabled", True) else "OFF (next cursor = new context)"
    return (
        "Project: %s\nPath: %s\nModel: %s\nMode: %s\nVoice: %s\nYOLO: %s\nContinue: %s"
        % (p, WORK_DIR / p if p != "none" else "-", mdl, m, voice, yolo, cont)
    )

def _help(bs):
    return """AVAILABLE COMMANDS:

PROJECTS:
  N (1-13)              - select project N
  projects / back       - show project list

CURSOR:
  cursor <prompt>       - send prompt to Cursor Agent (-p)
  new context / fresh   - next cursor starts without --continue

MODE:
  mode agent / plan / ask / shell - switch mode (new context)
  plan on / turn on plan          - plan mode (new context)
  plan off / turn off plan        - back to agent (new context)

SETTINGS:
  model <name>          - set Cursor model
  status                - current state
  yolo on / yolo off    - toggle --yolo for Cursor
  voice on / voice off  - toggle TTS for Cursor responses

LOCKS:
  lock                  - show lock status
  force                 - override stale lock

  help                  - show this message""" 

def _lock_status(bs):
    p = bs.get("project", "")
    if not p:
        return "No project selected."
    owner = _read_owner(WORK_DIR / p / ".agent-lock")
    if owner:
        return "Locked by: %s" % owner
    return "Not locked."

def _force_lock(bs):
    p = bs.get("project", "")
    if not p:
        return "No project selected."
    lockd = WORK_DIR / p / ".agent-lock"
    if lockd.exists():
        shutil.rmtree(lockd)
    _write_lock(lockd, bs.get("name", "daemon"))
    return "Lock acquired (forced)."

def _agent_env():
    """Env for Cursor agent subprocess; cap Node heap at 1GB."""
    env = {**os.environ, "HOME": "/home/ubuntu"}
    heap = "--max-old-space-size=1024"
    existing = env.get("NODE_OPTIONS", "").strip()
    if "--max-old-space-size=" in existing:
        parts = [p for p in existing.split() if not p.startswith("--max-old-space-size=")]
        parts.append(heap)
        env["NODE_OPTIONS"] = " ".join(parts)
    elif existing:
        env["NODE_OPTIONS"] = existing + " " + heap
    else:
        env["NODE_OPTIONS"] = heap
    return env

def _set_mode(bs, mode):
    """Set mode. Cursor sessions lock their mode, so a change among
    agent/plan/ask drops --continue (CLI has no --mode agent; agent = omit flag).
    Skip a second NEW banner if continue is already queued off."""
    prev = bs.get("current_mode", "agent")
    bs["current_mode"] = mode
    cursor_modes = ("agent", "plan", "ask")
    if mode in cursor_modes and prev != mode:
        already_new = not bs.get("continue_enabled", True)
        bs["continue_enabled"] = False
        if already_new:
            return "Mode: %s" % mode
        return (
            "Mode: %s\nContext: NEW (next cursor starts fresh in %s; "
            "then --continue resumes it)"
        ) % (mode, mode)
    return "Mode: %s" % mode

def _cursor(bs, text):
    p = bs.get("project", "")
    if not p:
        return "No project selected. Pick a number first."
    pd = WORK_DIR / p
    if not pd.is_dir():
        return "Project directory not found: %s" % pd
    mode = bs.get("current_mode", "agent")
    model = bs.get("current_model", "")
    if mode == "shell":
        cmd = ["bash", "-c", text]
        bs["_shell_cmd"] = shlex.join(cmd)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=30, cwd=str(pd), env={**os.environ, "HOME": "/home/ubuntu"})
            return r.stdout.strip() or r.stderr.strip() or "(empty)"
        except subprocess.TimeoutExpired:
            return "Command timed out (30s)."
    use_continue = bs.get("continue_enabled", True)
    cmd = [str(CLI), "-p"]
    if use_continue:
        cmd.append("--continue")
    if bs.get("yolo_enabled"):
        cmd.append("--yolo")
    if mode == "plan":
        cmd.append("--plan")
    elif mode == "ask":
        cmd.extend(["--mode", "ask"])
    # agent = omit --mode (CLI choices are only plan|ask; --continue of an
    # ask/plan session would keep that mode, so _set_mode drops continue)
    if model:
        cmd.extend(["--model", model])
    cmd.append(text)
    bs["_shell_cmd"] = shlex.join(cmd)
    # After first fresh prompt, resume this new context on later calls
    bs["continue_enabled"] = True
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                          cwd=str(pd), env=_agent_env())
        out = (r.stdout or r.stderr).strip() or "(empty response)"
        if not use_continue:
            out = "[new context]\n" + out
        return out
    except subprocess.TimeoutExpired:
        return "Cursor Agent timed out (120s)."
    except Exception as e:
        return "Error: %s" % e

def _cursor_prompt(text):
    """Return prompt text if this message invokes Cursor, else None."""
    raw_lower = text.lower()
    m = re.match(r'^cursor[,.:;!?\s]+(.+)', raw_lower)
    if m:
        return text[m.start(1):].strip()
    return None

def dispatch(s, bs, text):
    # Collapse internal whitespace so "mode    agent" matches "mode agent"
    lower = " ".join(text.lower().strip().rstrip(",.?!:;").split())
    # voice commands
    if lower == "voice on":
        bs["voice_enabled"] = True
        return "Voice: ON"
    if lower == "voice off":
        bs["voice_enabled"] = False
        return "Voice: OFF"
    # yolo commands
    if lower == "yolo on":
        bs["yolo_enabled"] = True
        return "YOLO: ON"
    if lower == "yolo off":
        bs["yolo_enabled"] = False
        return "YOLO: OFF"

    # cursor <prompt> → agent -p (before dict lookup)
    prompt = _cursor_prompt(text)
    if prompt is not None:
        bs["_speak"] = True
        return _cursor(bs, prompt)
    if lower == "cursor":
        return "Usage: cursor <prompt>"
    if lower.startswith("model "):
        mn = lower.split(" ", 1)[1] if " " in lower else ""
        if mn:
            bs["current_model"] = mn
            return "Model: %s" % mn
        return "Usage: model <name>"
    mappings = s.get("mappings", DM)
    op = mappings.get(lower)
    if not op:
        for k, v in mappings.items():
            if k.lower() == lower:
                op = v
                break
    if not op:
        return None
    if op == "display_projects":
        return _projects_list()
    elif op.startswith("select_project_"):
        return _select(bs, int(op.rsplit("_", 1)[-1]))
    elif op in ("mode_agent", "mode_plan", "mode_ask", "mode_shell"):
        return _set_mode(bs, op.split("_", 1)[-1])
    elif op == "mode_plan_on":
        return _set_mode(bs, "plan")
    elif op == "mode_plan_off":
        return _set_mode(bs, "agent")
    elif op == "display_status":
        return _status(bs)
    elif op == "display_help":
        return _help(bs)
    elif op == "display_lock":
        return _lock_status(bs)
    elif op == "force_lock":
        return _force_lock(bs)
    elif op == "new_context":
        bs["continue_enabled"] = False
        return "Context: NEW (next cursor starts fresh; then --continue resumes it)"
    elif op == "cursor_prompt":
        bs["_speak"] = True
        return _cursor(bs, text)
    return "Unknown op: %s" % op

# ─── Web UI ─────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html><head><title>Cursor Daemon</title>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font:13px/1.5 system-ui,sans-serif;background:#111;color:#ccc;max-width:900px;margin:0 auto;padding:16px}}
h1{{color:#0f0;margin-bottom:16px}}
.card{{background:#1a1a1a;border-radius:8px;padding:14px;margin-bottom:12px}}
.row{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
input,select,button{{padding:6px 10px;border:1px solid #333;border-radius:4px;background:#222;color:#ddd;font-size:12px}}
button{{background:#0a0;color:#000;cursor:pointer}}
button.danger{{background:#a00;color:#fff;font-size:11px;padding:4px 8px}}
button.small{{font-size:10px;padding:2px 6px}}
button.save{{background:#08f}}
select{{min-width:180px}}
.badge{{padding:2px 8px;border-radius:4px;font-size:11px}}
.badge-on{{background:#0a0;color:#000}}
.log-head{{display:flex;align-items:center;gap:8px;margin-bottom:8px}}
.log-head h3{{margin:0}}
.log-hint{{color:#666;font-size:11px}}
.log-card{{width:calc(100vw - 32px);max-width:none;margin-left:calc(50% - 50vw + 16px);box-sizing:border-box}}
.log-entry{{font:10px monospace;padding:1px 0;border-bottom:1px solid #222;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.log-error{{color:#f44}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th,td{{padding:4px 8px;text-align:left;border-bottom:1px solid #222}}
th{{color:#888;font-weight:normal}}
td input{{width:100%;background:#111;border:1px solid #333;padding:3px 6px}}
</style></head><body>
<h1>🖥️ Cursor Daemon</h1>

<div class="card">
<h3>➕ Add Bot</h3>
<form class="row" method="POST" action="/add">
<input name="name" placeholder="Bot name (e.g. cursor-3)" required style="flex:1">
<input name="token" placeholder="Telegram bot token" required style="flex:2">
<select name="project"><option value="">— Select project —</option>
{project_options}
</select>
<button type="submit">Add</button>
</form>
</div>

{bot_cards}

<div class="card log-card">
<div class="log-head">
<h3>📋 Recent Logs</h3>
<button type="button" class="small" onclick="location.reload()">Refresh</button>
<span class="log-hint">(r)</span>
</div>
{log_entries}
</div>

<script>
document.addEventListener("keydown",function(e){{
  if(e.key!=="r"&&e.key!=="R")return;
  var t=e.target&&e.target.tagName;
  if(t==="INPUT"||t==="SELECT"||t==="TEXTAREA")return;
  e.preventDefault();
  location.reload();
}});
</script>
</body></html>"""

BOT_CARD = """<div class="card">
<h3>🤖 {name} <span class="badge badge-on">● {project}</span></h3>
<div class="row" style="margin-bottom:8px">
<form method="POST" action="/assign">
<input type="hidden" name="token" value="{token}">
<select name="project" onchange="this.form.submit()">
<option value="">— Unassigned —</option>
{proj_opts}
</select>
</form>
<form method="POST" action="/remove" onsubmit="return confirm('Delete {name}?')">
<input type="hidden" name="token" value="{token}">
<button class="danger">✕</button>
</form>
</div>

</div>"""

async def web_ui(request):
    from aiohttp import web
    s = load_state()
    projects = sorted([d.name for d in WORK_DIR.iterdir() if d.is_dir() and not d.name.startswith(".")])
    proj_opts = "\n".join('<option value="%s">%s</option>' % (p, p) for p in projects)
    op_opts = "\n".join('<option value="%s">%s</option>' % (o, lb) for o, lb in OPS)
    
    cards = []
    for tok, bot in s.get("bots", {}).items():
        name = html.escape(bot.get("name", "?"))
        proj = html.escape(bot.get("project", "-"))
        # Build selected project options
        if proj != "-":
            psel = proj_opts.replace('value="%s"' % proj, 'value="%s" selected' % proj)
        else:
            psel = proj_opts
        
        mappings = bot.get("mappings", {})
        rows = ""
        for cmd, op_v in sorted(mappings.items()):
            sel = op_opts.replace('value="%s"' % op_v, 'value="%s" selected' % op_v)
            ec = html.escape(cmd)
            rows += '<tr>'
            rows += '<td><input name="cmd_%s" value="%s"></td>' % (ec, ec)
            rows += '<td><select name="op_%s">%s</select></td>' % (ec, sel)
            rows += '<td><button class="danger small" formmethod="POST" formaction="/delete_mapping" name="cmd" value="%s">X</button></td>' % ec
            rows += '<input type="hidden" name="token" value="%s">' % html.escape(tok)
            rows += '</tr>'
        
        card = BOT_CARD.format(
            name=name, token=html.escape(tok), project=proj,
            proj_opts=psel, map_count=len(mappings),
            mapping_rows=rows, op_options=op_opts
        )
        cards.append(card)
    
    # Global mappings card
    map_rows = ""
    global_maps = s.get("mappings", DM)
    for cmd, op_v in sorted(global_maps.items()):
        sel = op_opts.replace('value="%s"' % op_v, 'value="%s" selected' % op_v)
        ec = html.escape(cmd)
        map_rows += '<tr>'
        map_rows += '<td><input name="cmd_%s" value="%s"></td>' % (ec, ec)
        map_rows += '<td><select name="op_%s">%s</select></td>' % (ec, sel)
        map_rows += '<td><button class="danger small" formmethod="POST" formaction="/delete_mapping" name="cmd" value="%s">X</button></td>' % ec
        map_rows += '</tr>'
    cards.append('''<div class="card">
<h3>📖 Global Mappings (%d)</h3>
<form method="POST" action="/save_mappings" style="margin-top:8px">
<table>
<tr><th>Telegram command</th><th>→ Operation</th><th></th></tr>
%s
</table>
<div style="margin-top:8px;display:flex;gap:8px">
<input name="new_cmd" placeholder="New command" style="width:140px">
<select name="new_op">%s</select>
<button type="submit" name="action" value="add">+ Add</button>
<button type="submit" name="action" value="save" class="save">💾 Save All</button>
</div>
</form>
</div>''' % (len(global_maps), map_rows, op_opts))
    
    logs = s.get("logs", [])[-40:]
    log_html = ""
    for e in reversed(logs):
        cls = "log-error" if e.get("level") == "error" else ""
        t = e.get("time", "")[11:19]
        log_html += '<div class="log-entry %s">%s %s</div>\n' % (cls, t, html.escape(e.get("msg", "")))
    
    html_content = HTML.format(
        project_options=proj_opts,
        bot_cards="\n".join(cards) or "<p style='color:#888'>No bots added.</p>",
        log_entries=log_html or "<p style='color:#888'>No logs yet.</p>"
    )
    return web.Response(text=html_content, content_type="text/html")

async def web_add(request):
    from aiohttp import web
    d = await request.post()
    name = (d.get("name") or "").strip()
    tok = (d.get("token") or "").strip()
    proj = (d.get("project") or "").strip()
    if not name or not tok:
        return web.Response(text="Name and token required", status=400)
    s = load_state()
    s["bots"][tok] = {"name": name, "project": proj, "last_command": "", "mappings": dict(DM)}
    save_state(s)
    log(s, "Bot added: %s" % name)
    raise web.HTTPFound("/")

async def web_remove(request):
    from aiohttp import web
    d = await request.post()
    tok = (d.get("token") or "").strip()
    s = load_state()
    if tok in s.get("bots", {}):
        name = s["bots"][tok]["name"]
        del s["bots"][tok]
        save_state(s)
        log(s, "Bot removed: %s" % name)
    raise web.HTTPFound("/")

async def web_assign(request):
    from aiohttp import web
    d = await request.post()
    tok = (d.get("token") or "").strip()
    proj = (d.get("project") or "").strip()
    s = load_state()
    if tok not in s.get("bots", {}):
        raise web.HTTPFound("/")
    bot = s["bots"][tok]
    old_proj = bot.get("project", "")
    bn = bot.get("name", "daemon")

    if not proj:
        _release_lock_if_ours(old_proj, bn)
        bot["project"] = ""
        save_state(s)
        log(s, "[%s] Assigned to unassigned" % bn)
        raise web.HTTPFound("/")

    if old_proj == proj:
        dest_owner = _read_owner(WORK_DIR / proj / ".agent-lock")
        if dest_owner and dest_owner != bn:
            log(s, "[%s] WARNING: %s locked by %s" % (bn, proj, dest_owner))
        else:
            _write_lock(WORK_DIR / proj / ".agent-lock", bn)
        save_state(s)
        raise web.HTTPFound("/")

    dest_owner = _read_owner(WORK_DIR / proj / ".agent-lock")
    if dest_owner and dest_owner != bn:
        log(s, "[%s] WARNING: %s locked by %s — assignment blocked" % (bn, proj, dest_owner))
        raise web.HTTPFound("/")

    _release_lock_if_ours(old_proj, bn)
    _write_lock(WORK_DIR / proj / ".agent-lock", bn)
    bot["project"] = proj
    save_state(s)
    log(s, "[%s] Assigned to %s" % (bn, proj))
    raise web.HTTPFound("/")

async def web_save_mappings(request):
    from aiohttp import web
    d = await request.post()
    tok = (d.get("token") or "").strip()
    action = d.get("action", "save")
    s = load_state()
    mappings = s.get("mappings", dict(DM))
    
    if action == "add":
        cmd = (d.get("new_cmd") or "").strip().lower()
        op = (d.get("new_op") or "").strip()
        if cmd and op:
            mappings[cmd] = op
            log(s, "Mapped: '%s' -> %s" % (cmd, op))
    elif action == "save":
        new_m = {}
        for key in d:
            if key.startswith("cmd_") and d[key].strip():
                cmd_name = key[4:]
                op_key = "op_%s" % cmd_name
                op_val = (d.get(op_key) or "").strip()
                if op_val:
                    new_m[cmd_name] = op_val
        if new_m:
            mappings = new_m
            log(s, "Mappings saved")
    s["mappings"] = mappings
    save_state(s)
    raise web.HTTPFound("/")

async def web_delete_mapping(request):
    from aiohttp import web
    d = await request.post()
    tok = (d.get("token") or "").strip()
    cmd = (d.get("cmd") or "").strip().lower()
    s = load_state()
    if cmd in s.get("mappings", {}):
        del s["mappings"][cmd]
        save_state(s)
        log(s, "Mapping deleted: '%s'" % cmd)
    raise web.HTTPFound("/")

# ─── Telegram ───────────────────────────────────────────────────────

async def send_tg(token, chat_id, text):
    import aiohttp
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                "https://api.telegram.org/bot%s/sendMessage" % token,
                json={"chat_id": chat_id, "text": text[:4096]},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                return await resp.json()
    except Exception as e:
        print("Telegram error: %s" % e)

def _tts_plain(text):
    """Strip light markdown/code noise so TTS sounds cleaner."""
    t = (text or "").strip()
    if not t:
        return ""
    t = re.sub(r"```[\s\S]*?```", " ", t)
    t = re.sub(r"`([^`]+)`", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"[*_~>#]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > TTS_MAX_CHARS:
        t = t[:TTS_MAX_CHARS] + "…"
    return t

async def _edge_tts_ogg(text):
    """Synthesize text via edge-tts → mp3 → ogg/opus. Returns ogg path or None."""
    import edge_tts
    speak = _tts_plain(text)
    if not speak:
        return None
    tmp = tempfile.mkdtemp(prefix="cursor-tts-")
    mp3_path = os.path.join(tmp, "speech.mp3")
    ogg_path = os.path.join(tmp, "speech.ogg")
    try:
        await edge_tts.Communicate(speak, EDGE_VOICE).save(mp3_path)
        r = await asyncio.to_thread(
            subprocess.run,
            ["ffmpeg", "-i", mp3_path, "-acodec", "libopus",
             "-ac", "1", "-b:a", "48k", "-vbr", "on",
             "-application", "voip", "-compression_level", "10",
             "-f", "ogg", ogg_path, "-y"],
            capture_output=True, timeout=30, stdin=subprocess.DEVNULL,
        )
        if r.returncode == 0 and os.path.exists(ogg_path) and os.path.getsize(ogg_path) > 0:
            return ogg_path
        err = (r.stderr or b"")[:200]
        print("ffmpeg TTS failed: %s" % err)
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    except Exception as e:
        print("TTS error: %s" % e)
        shutil.rmtree(tmp, ignore_errors=True)
        return None

async def send_tg_voice(token, chat_id, text):
    """Send Cursor reply as Telegram voice note. Falls back silently on failure."""
    import aiohttp
    ogg = await _edge_tts_ogg(text)
    if not ogg:
        return None
    try:
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        with open(ogg, "rb") as fh:
            form.add_field(
                "voice", fh, filename="reply.ogg", content_type="audio/ogg",
            )
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    "https://api.telegram.org/bot%s/sendVoice" % token,
                    data=form,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    return await resp.json()
    except Exception as e:
        print("Telegram voice error: %s" % e)
        return None
    finally:
        shutil.rmtree(os.path.dirname(ogg), ignore_errors=True)

async def handle_message(token, bot_name, text, chat_id, state):
    bot = bot_state(state, token)
    speak = False
    async with _state_lock:
        result = dispatch(state, bot, text)
        speak = bool(bot.pop("_speak", False) and bot.get("voice_enabled"))
        shell_cmd = bot.pop("_shell_cmd", None)
        if result is not None:
            bot["last_command"] = text
            proj = bot.get("project", "-")
            detail = shell_cmd if shell_cmd else text
            log(state, "[%s:%s] %s" % (bot_name, proj, detail))
            save_state(state)
    if result is not None:
        await send_tg(token, chat_id, result)
        if speak:
            await send_tg_voice(token, chat_id, result)
    else:
        log(state, "[%s] Unknown: %s" % (bot_name, text[:50]))
        await send_tg(token, chat_id,
            "Unknown command: %s\n\nType 'help' for available commands." % text)

async def poll_bot(token, state):
    import aiohttp
    offset = 0
    url_tmpl = "https://api.telegram.org/bot%s/getUpdates" % token
    info = state["bots"].get(token, {})
    name = info.get("name", "?")
    while True:
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url_tmpl, params={
                    "offset": offset, "timeout": 30, "allowed_updates": ["message"]
                }, timeout=aiohttp.ClientTimeout(total=35)) as resp:
                    data = await resp.json()
            if data.get("ok"):
                for u in data["result"]:
                    offset = u["update_id"] + 1
                    msg = u.get("message", {})
                    txt = msg.get("text", "")
                    cid = msg.get("chat", {}).get("id")
                    if txt and cid:
                        await handle_message(token, name, txt, cid, state)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print("Poll error %s: %s" % (name, e))
            await asyncio.sleep(5)

# ─── Main ───────────────────────────────────────────────────────────

async def main():
    from aiohttp import web
    s = load_state()
    log(s, "Daemon v2 starting...")
    
    app = web.Application()
    app.router.add_get("/", web_ui)
    app.router.add_post("/add", web_add)
    app.router.add_post("/remove", web_remove)
    app.router.add_post("/assign", web_assign)
    app.router.add_post("/save_mappings", web_save_mappings)
    app.router.add_post("/delete_mapping", web_delete_mapping)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()
    log(s, "Web UI: http://%s:%d" % (HOST, PORT))
    
    tasks = []
    for tok in list(s.get("bots", {}).keys()):
        tasks.append(asyncio.create_task(poll_bot(tok, s)))
    log(s, "Polling %d bot(s)" % len(tasks))
    
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        log(s, "Shutting down...")
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())