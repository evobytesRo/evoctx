"""
Context store CLI (`evoctx`).

Mirrors of the MCP tools — everything the AI can do, you can do:

  evoctx list-projects                                        Same as MCP list_projects()
  evoctx get-project NAME                                     Same as MCP get_project()
  evoctx search-context QUERY [--project NAME] [--limit N]    Same as MCP search_context()
  evoctx get-by-id ID                                         Same as MCP get_by_id() (alias: show)
  evoctx write-note "Title" --project NAME --tags "#decision" Same as MCP write_note() (alias: add)
  evoctx start-session [--project NAME] [--intent "..."]      Same as MCP start_session()
  evoctx end-session "summary" [--id ID]                      Same as MCP end_session() (default: latest open)
  evoctx recent-sessions [--project NAME] [--limit N]         Same as MCP recent_sessions()
  evoctx list-grants [--client NAME]                          Same as MCP list_grants()

Grant management (human-only):

  evoctx grant list                                           All grants defined in grants.yaml
  evoctx grant show NAME                                      One grant in detail
  evoctx grant activate NAME                                  Make NAME the active grant
  evoctx grant deactivate                                     No active grant — every call denied

Audit (human-only — deliberately not an MCP tool):

  evoctx audit [--client C] [--tool T] [--action A] [--since 24h] [--limit N]
                                                              What did which client read/write/search, when
  evoctx audit-prune [--older-than 90d]                       Delete old audit entries (bypasses append-only
                                                              on purpose, for this one human-only operation)

Setup:

  evoctx install <client> [--uvx]                             Write MCP config for: claude-code,
                                                              claude-desktop, cursor, windsurf, vscode, codex
                                                              (claude-code is project-scoped — see --dir)
  evoctx doctor                                               Verify store, grants, server, client configs
  evoctx --version | evoctx version                           Show the installed version

Store management (human-only, no MCP equivalent):

  evoctx init [--dir PATH] [--name NAME] [--template NAME]   Register project from directory
  evoctx new-project NAME --overview "..."                    Register project manually
  evoctx new-template NAME --overview "..."                   Create a reusable template
  evoctx templates                                            List available templates
  evoctx import-dir path/to/notes/ --project NAME
  evoctx update-project NAME --field conventions --file conventions.md
  evoctx list [--project NAME]                                List notes
  evoctx delete ID | --project NAME | --tag TAG               Delete note(s). Human-only, no MCP tool.
  evoctx delete-project NAME [--with-notes]                   Delete a project. Human-only, no MCP tool.
  evoctx export [--format json|markdown] [PATH]               Export the whole store
  evoctx dump [--project NAME] [--note ID]                    Read-only store dump
  evoctx sync [PATH]                                          Sync a markdown folder into the store
  evoctx demo-seed [--force]                                  Populate sample data to try things out

Templates are stored as projects prefixed with '_' (e.g. _fastapi, _react-vite).
Use --template on init/new-project to stamp a project with a template's conventions.

Examples:

  evoctx list-projects
  evoctx get-project my-api
  evoctx search "webhook retry" --project my-api --limit 5
  evoctx show 61ba577b
  evoctx add "Decision: use Redis for rate limiting" -p my-api -t "#decision" --text "Because ..."
  evoctx add "Meeting notes" -p my-api --file notes.md
  evoctx init --dir C:/code/my-api --template fastapi
  evoctx sync ./context

Every command also accepts -h for its own flags (e.g. evoctx add -h).
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

from .db import Database, NoteTooLarge
from .grants import GrantError, GrantStore
from .paths import store_dir
from .policy import PolicyEngine

STORE_DIR = store_dir()
db = Database(STORE_DIR)
grant_store = GrantStore(STORE_DIR)
policy = PolicyEngine(db, grant_store)

TEMPLATE_PREFIX = "_"


def _template_name(name: str) -> str:
    return name if name.startswith(TEMPLATE_PREFIX) else f"{TEMPLATE_PREFIX}{name}"


def _merge_conventions(template: str, project: str) -> str:
    if not template and not project:
        return ""
    if not template:
        return project
    if not project:
        return template
    return f"{template}\n\n---\n\n## Project-specific\n\n{project}"


# ── MCP tool mirrors ──────────────────────────────────────────────────────────

def cmd_list_projects(args):
    names = db.list_projects()
    if not names:
        print("No projects registered. Start with: evoctx init")
        return
    for name in names:
        proj = db.get_project(name)
        overview = (proj["overview"][:70] + "…") if proj and len(proj["overview"]) > 70 else (proj["overview"] if proj else "")
        print(f"  {name:<25} {overview}")
    print(f"\n{len(names)} project(s)")


def cmd_get_project(args):
    proj = db.get_project(args.name)
    if not proj:
        print(f"Project '{args.name}' not found. See: evoctx list-projects")
        sys.exit(1)
    print(f"Name:    {proj['name']}")
    print(f"Updated: {proj['updated_at']}")
    print()
    print("── Overview " + "─" * 48)
    print(proj["overview"] or "(empty)")
    print()
    print("── Conventions " + "─" * 45)
    print(proj["conventions"] or "(empty)")
    print()
    print("── Open questions " + "─" * 42)
    print(proj["open_questions"] or "(empty)")
    print()
    print("── Recent notes " + "─" * 44)
    if not proj["recent_notes"]:
        print("(none)")
    for n in proj["recent_notes"]:
        tags = " ".join(n["tags"])
        print(f"  {n['id'][:8]}  {n['title']}  {tags}")


def cmd_search_context(args):
    results = db.search(args.query, project=args.project, limit=args.limit)
    if not results:
        print("No matches.")
        return
    for r in results:
        tags = " ".join(r["tags"])
        print(f"  {r['id'][:8]}  [{r['project'] or '—'}]  {r['title']}  {tags}")
        print(f"           {r['snippet']}")
        print()
    print(f"{len(results)} result(s). Full note: evoctx get-by-id <ID>")


def _print_session(s: dict):
    state = "OPEN" if s["open"] else "closed"
    print(f"  {s['id'][:8]}  [{s['project'] or '—'}]  {state}  client={s['client']}")
    print(f"           started: {s['started_at']}" + (f"  ended: {s['ended_at']}" if s["ended_at"] else ""))
    if s["intent"]:
        print(f"           intent:  {s['intent']}")
    if s["summary"]:
        print(f"           summary: {s['summary'][:120]}")
    if s["notes"]:
        for n in s["notes"]:
            print(f"             note {n['id'][:8]}  {n['title']}")
    print()


def cmd_start_session(args):
    import os
    session_id = db.start_session(
        project=args.project, intent=args.intent,
        client=os.environ.get("CONTEXT_CLIENT", "cli"),
    )
    print(f"Started session: {session_id}")
    print(f"Close with: evoctx end-session \"summary\" --id {session_id[:8]}")


def cmd_end_session(args):
    if args.id:
        session = db.get_session(args.id) or next(
            (s for s in db.recent_sessions(limit=50) if s["id"].startswith(args.id)), None
        )
    else:
        session = db.latest_open_session(project=args.project)
    if not session:
        print("No matching open session found. See: evoctx recent-sessions")
        sys.exit(1)
    if not db.end_session(session["id"], args.summary):
        print(f"Session {session['id'][:8]} is already closed.")
        sys.exit(1)
    print(f"Closed session {session['id'][:8]}.")


def cmd_recent_sessions(args):
    sessions = db.recent_sessions(project=args.project, limit=args.limit)
    if not sessions:
        print("No sessions recorded yet.")
        return
    for s in sessions:
        _print_session(s)
    print(f"{len(sessions)} session(s)")


def cmd_list_grants(args):
    import json
    print(json.dumps(policy.describe(args.client), indent=2))


def cmd_grant(args):
    if args.action == "list":
        grant, expires_at, reason = grant_store.active_grant()
        active_name = grant.name if grant else None
        for name, g in grant_store.grants().items():
            marker = "* " if name == active_name else "  "
            print(f"{marker}{name:<20} client={g.client:<12} tools={len(g.tools)} "
                  f"projects={g.projects} expires={g.expires}")
        if grant:
            print(f"\nActive: {active_name} (expires {expires_at.isoformat()})")
        else:
            print(f"\nNo active grant — {reason}")
        print(f"Config: {grant_store.grants_path}")

    elif args.action == "show":
        if not args.name:
            print("Usage: evoctx grant show NAME")
            sys.exit(1)
        g = grant_store.grants().get(args.name)
        if not g:
            print(f"No grant named '{args.name}'. See: evoctx grant list")
            sys.exit(1)
        print(f"Name:         {g.name}")
        print(f"Client:       {g.client}")
        print(f"Tools:        {', '.join(g.tools)}")
        print(f"Projects:     {g.projects}")
        print(f"Exclude tags: {g.exclude_tags}")
        print(f"Redactions:   {len(g.redactions)} pattern(s)")
        print(f"Expires:      {g.expires}")

    elif args.action == "activate":
        if not args.name:
            print("Usage: evoctx grant activate NAME")
            sys.exit(1)
        try:
            g = grant_store.activate(args.name)
        except GrantError as e:
            print(f"Error: {e}")
            sys.exit(1)
        _, expires_at, _ = grant_store.active_grant()
        print(f"Activated grant '{g.name}' (expires {expires_at.isoformat()})")

    elif args.action == "deactivate":
        grant_store.deactivate()
        print("Deactivated. Every MCP call is now denied until a grant is activated.")


def cmd_audit(args):
    import json
    from .audit import query_audit
    try:
        entries = query_audit(
            db.conn, client=args.client, tool=args.tool, action=args.action,
            since=args.since, limit=args.limit,
        )
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)
    if not entries:
        print("No audit entries match.")
        return
    for e in entries:
        q = json.dumps(e["query"]) if e["query"] else ""
        print(f"  {e['ts'][:19]}  {e['client']:<12} {e['action']:<7} {e['tool']:<16} "
              f"n={e['result_count']:<3} {e['response_bytes']}B"
              + (f"  redactions={e['redactions']}" if e["redactions"] else ""))
        if q:
            print(f"    {q}")
        if e["record_ids"]:
            print(f"    records: {', '.join(str(r)[:8] for r in e['record_ids'][:6])}"
                  + (" …" if len(e["record_ids"]) > 6 else ""))
    print(f"\n{len(entries)} entrie(s), newest first")


def cmd_audit_prune(args):
    from .audit import parse_since, prune_audit_log
    try:
        cutoff = parse_since(args.older_than)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)
    count = db.conn.execute("SELECT count(*) FROM audit_log WHERE ts < ?", (cutoff,)).fetchone()[0]
    if count == 0:
        print(f"No audit entries older than {args.older_than}.")
        return
    if not _confirm(
        f"Delete {count} audit entrie(s) older than {args.older_than}? "
        f"This bypasses the append-only guard for this one operation. Cannot be undone. (y/n)",
        args.force,
    ):
        print("Cancelled.")
        return
    n = prune_audit_log(db.conn, cutoff)
    print(f"Pruned {n} audit entrie(s) older than {args.older_than}.")


def cmd_install(args):
    import json
    from pathlib import Path as _Path

    from .install import (
        NEXT_STEPS, SERVER_NAME, claude_cli_hint, clients, install_claude_code,
        install_claude_code_hooks, install_claude_code_permissions_deny,
        install_codex, install_json_client, server_command,
    )

    home = _Path.home()
    if args.hooks and args.client != "claude-code":
        print("--hooks is only supported for claude-code.")
        sys.exit(1)

    if args.client == "claude-code":
        project_dir = _Path(args.dir) if args.dir else _Path.cwd()
        path, entry = install_claude_code(project_dir, uvx=args.uvx)
        print(f"Wrote {path} (backup: .bak):")
        print(json.dumps({SERVER_NAME: entry}, indent=2))
        print()
        print("Claude Code (VS Code extension) reads MCP servers from a project-scoped")
        print(".mcp.json, not a global user file — re-run this in every project/workspace")
        print("root you want evoctx available in.")
        command, cargs = server_command()
        hint = claude_cli_hint(command, cargs)
        if hint:
            print()
            print("Standalone `claude` CLI detected — for one-time global registration")
            print("across every project instead, you can also run:")
            print(f"  {hint}")
    elif args.client == "codex":
        block = install_codex(home, uvx=args.uvx)
        print(f"Wrote {home / '.codex' / 'config.toml'}:")
        print(block)
        print()
        print(NEXT_STEPS["codex-uvx"] if args.uvx else NEXT_STEPS["codex"])
    else:
        registry = clients(home)
        client = registry.get(args.client)
        if not client:
            print(f"Unknown client '{args.client}'. "
                  f"Supported: claude-code, {', '.join(registry)}, codex")
            print("(ChatGPT Desktop does not support local stdio MCP servers.)")
            sys.exit(1)
        entry = install_json_client(client, uvx=args.uvx)
        print(f"Wrote {client.config_path} (backup: .bak):")
        print(json.dumps({SERVER_NAME: entry}, indent=2))
        tip = NEXT_STEPS.get(args.client)
        if tip:
            print()
            print(tip)

    if args.hooks:
        hooks = install_claude_code_hooks(home)
        print()
        print(f"Wrote hooks into {home / '.claude' / 'settings.json'} (backup: .bak):")
        print(json.dumps(hooks, indent=2))
        print()
        print("SessionStart nudges start_session/recent_sessions; Stop blocks once (never twice)")
        print("if a session opened in the last 12h was never closed with end_session.")

        deny = install_claude_code_permissions_deny(home, STORE_DIR)
        print()
        print("Also added permissions.deny rules blocking the assistant's own file tools")
        print(f"from reading/editing the store directly ({STORE_DIR}):")
        print(json.dumps(deny, indent=2))
        print("Best-effort — this is Claude Code's permission syntax as of writing, not")
        print("something this project controls the stability of. Confirm it actually takes")
        print("effect (e.g. ask the assistant to read a file under the store and expect a denial).")

    print()
    print("Restart the client, then verify with: evoctx doctor")
    print("Add the instructions snippet to the client's rules file — see docs/instructions-snippet.md")


def cmd_doctor(args):
    from pathlib import Path as _Path

    from .install import doctor

    failed = False
    for check, ok, detail in doctor(STORE_DIR, _Path.home()):
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failed = True
        print(f"  [{mark}] {check:<15} {detail}")
    sys.exit(1 if failed else 0)


def cmd_hook(args):
    """Claude Code hook entry points — not for humans to run directly.
    Written into settings.json by 'evoctx install claude-code --hooks'."""
    import json as _json
    from datetime import datetime, timezone

    stdin_data = {}
    try:
        stdin_data = _json.load(sys.stdin)
    except Exception:
        pass

    if args.event == "session-start":
        print(
            "evoctx: call start_session(project, intent), then recent_sessions() "
            "before starting work — open entries there were likely interrupted."
        )
        return

    if args.event == "stop":
        # Claude Code sets this when it already blocked once this stop cycle —
        # never trap the user in a loop over one forgotten end_session call.
        if stdin_data.get("stop_hook_active"):
            return
        session = db.latest_open_session()
        if not session:
            return
        started = datetime.fromisoformat(session["started_at"])
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - started).total_seconds() / 3600
        if age_hours <= 12:
            print(
                f"Open session {session['id'][:8]} (intent: {session.get('intent') or '-'}) "
                f"has no end_session() call yet. Call end_session(summary) to close it, "
                f"or ignore this message to stop anyway.",
                file=sys.stderr,
            )
            sys.exit(2)


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_add(args):
    if args.file:
        content = Path(args.file).read_text(encoding="utf-8")
    elif args.text:
        content = args.text
    else:
        print("Content (Ctrl+Z / Ctrl+D to finish):")
        content = sys.stdin.read()

    tags = [t.strip() for t in args.tags.split(",")] if args.tags else []
    from .secrets import redact_secrets
    _, hits = redact_secrets(content)
    try:
        note_id = db.add_note(args.title, content, project=args.project, tags=tags, author_client="cli")
    except NoteTooLarge as e:
        print(f"Error: {e}")
        sys.exit(1)
    print(f"Added: {note_id}")
    print(f"  title:   {args.title}")
    print(f"  project: {args.project or '-'}")
    print(f"  tags:    {tags}")
    if hits:
        print(f"  note: {hits} likely secret(s) were masked before storing")


def cmd_import_dir(args):
    folder = Path(args.path)
    if not folder.is_dir():
        print(f"Not a directory: {folder}")
        sys.exit(1)

    tags = [t.strip() for t in args.tags.split(",")] if args.tags else []
    files = list(folder.glob("*.md")) + list(folder.glob("*.txt"))
    if not files:
        print("No .md or .txt files found.")
        return

    for f in sorted(files):
        title = f.stem.replace("-", " ").replace("_", " ").title()
        content = f.read_text(encoding="utf-8")
        try:
            note_id = db.add_note(title, content, project=args.project, tags=tags, author_client="cli")
        except NoteTooLarge as e:
            print(f"  {f.name} -> SKIPPED: {e}")
            continue
        print(f"  {f.name} -> {note_id[:8]} ({title})")

    print(f"\nImported {len(files)} file(s).")


def cmd_update_project(args):
    proj = db.get_project(args.name)
    if not proj:
        print(f"Project '{args.name}' not found. Create it? (y/n)", end=" ")
        if input().strip().lower() != "y":
            return
        proj = {"overview": "", "conventions": "", "open_questions": ""}

    if args.file:
        value = Path(args.file).read_text(encoding="utf-8")
    elif args.text:
        value = args.text.replace("\\n", "\n")
    else:
        print(f"New {args.field} (Ctrl+Z / Ctrl+D to finish):")
        value = sys.stdin.read()

    proj[args.field] = value
    db.upsert_project(
        args.name,
        overview=proj["overview"],
        conventions=proj["conventions"],
        open_questions=proj["open_questions"],
    )
    print(f"Updated {args.name}.{args.field}")


def cmd_new_project(args):
    conventions = ""
    if args.template:
        tname = _template_name(args.template)
        tmpl = db.get_project(tname)
        if not tmpl:
            print(f"Template '{tname}' not found. Run: evoctx templates")
            sys.exit(1)
        conventions = _merge_conventions(tmpl["conventions"], args.conventions or "")
        print(f"Stamped with template: {tname}")
    else:
        conventions = args.conventions or ""

    existing = db.get_project(args.name)
    if existing:
        print(f"Project '{args.name}' already exists. Use update-project to change fields.")
        return

    db.upsert_project(
        args.name,
        overview=args.overview or "",
        conventions=conventions,
        open_questions=args.open_questions or "",
    )
    print(f"Registered project: {args.name}")


def cmd_new_template(args):
    name = _template_name(args.name)
    existing = db.get_project(name)
    if existing:
        print(f"Template '{name}' already exists. Use update-project to edit it.")
        return

    if args.file:
        conventions = Path(args.file).read_text(encoding="utf-8")
    else:
        conventions = args.conventions or ""

    db.upsert_project(name, overview=args.overview or "", conventions=conventions)
    print(f"Created template: {name}")
    print(f"Edit with: evoctx update-project {name} --field conventions --file path/to/conventions.md")


def cmd_templates(args):
    projects = db.list_projects()
    templates = [p for p in projects if p.startswith(TEMPLATE_PREFIX)]
    if not templates:
        print("No templates found. Create one with: evoctx new-template NAME")
        return
    print("Available templates:")
    for t in templates:
        proj = db.get_project(t)
        print(f"  {t:<25} {proj['overview'][:70] if proj else ''}")


def cmd_init(args):
    import json as _json

    directory = Path(args.dir).resolve() if args.dir else Path.cwd()
    name = args.name or directory.name

    if not directory.is_dir():
        print(f"Directory not found: {directory}")
        sys.exit(1)

    overview_lines: list[str] = []
    project_conventions = ""
    stack_hint = ""

    # --- detect stack from manifest files ---
    pkg_json = directory / "package.json"
    pyproject = directory / "pyproject.toml"
    cargo = directory / "Cargo.toml"

    if pkg_json.exists():
        try:
            pkg = _json.loads(pkg_json.read_text(encoding="utf-8"))
            if pkg.get("description"):
                overview_lines.append(pkg["description"])
            deps = list(pkg.get("dependencies", {}).keys()) + list(pkg.get("devDependencies", {}).keys())
            notable = [d for d in deps if d in (
                "react", "next", "vue", "svelte", "express", "fastify",
                "vite", "tailwindcss", "@tanstack/react-query", "prisma",
            )]
            if notable:
                stack_hint = "Stack: " + ", ".join(notable)
        except Exception:
            pass

    if pyproject.exists():
        try:
            text = pyproject.read_text(encoding="utf-8")
            for line in text.splitlines():
                if line.strip().startswith("description"):
                    desc = line.split("=", 1)[-1].strip().strip('"').strip("'")
                    if desc:
                        overview_lines.append(desc)
                    break
            for keyword in ("fastapi", "django", "flask", "sqlalchemy", "pydantic", "alembic"):
                if keyword in text.lower():
                    stack_hint = (stack_hint + f", {keyword}").lstrip(", ")
        except Exception:
            pass

    if cargo.exists():
        try:
            text = cargo.read_text(encoding="utf-8")
            for line in text.splitlines():
                if line.strip().startswith("description"):
                    desc = line.split("=", 1)[-1].strip().strip('"')
                    if desc:
                        overview_lines.append(desc)
                    break
            stack_hint = "Rust"
        except Exception:
            pass

    # --- pull first paragraph from CLAUDE.md or README ---
    for candidate in ("CLAUDE.md", "README.md", "readme.md", "Readme.md"):
        f = directory / candidate
        if f.exists():
            try:
                text = f.read_text(encoding="utf-8")
                para = []
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    if stripped:
                        para.append(stripped)
                    elif para:
                        break
                if para:
                    overview_lines.append(" ".join(para))
                    if candidate == "CLAUDE.md":
                        project_conventions = f"See CLAUDE.md at {f}"
                break
            except Exception:
                pass

    overview = " | ".join(dict.fromkeys(overview_lines)) if overview_lines else f"Project at {directory}"
    if stack_hint:
        overview = f"{overview} [{stack_hint}]"

    # --- apply template if requested ---
    conventions = project_conventions
    if args.template:
        tname = _template_name(args.template)
        tmpl = db.get_project(tname)
        if not tmpl:
            print(f"Template '{tname}' not found. Run: evoctx templates")
            sys.exit(1)
        conventions = _merge_conventions(tmpl["conventions"], project_conventions)
        print(f"Stamped with template: {tname}")

    existing = db.get_project(name)
    if existing and not args.force:
        print(f"Project '{name}' already registered. Use --force to overwrite.")
        print(f"  overview: {existing['overview'][:80]}")
        return

    db.upsert_project(name, overview=overview, conventions=conventions)
    print(f"Registered project: {name}")
    print(f"  directory: {directory}")
    print(f"  overview:  {overview}")
    print()
    print(f"Refine with:")
    print(f"  evoctx update-project {name} --field conventions --text \"...\"")
    print(f"  evoctx update-project {name} --field open_questions --text \"...\"")


def cmd_list(args):
    rows = db.list_notes(project=args.project)
    print(f"{'PROJECT':<22} {'TITLE':<50} {'TAGS'}")
    print("-" * 90)
    for r in rows:
        tags = " ".join(r["tags"])
        print(f"  {(r['project'] or '-'):<20} {r['title']:<50} {tags}")
    print(f"\n{len(rows)} note(s)")


def cmd_show(args):
    note = db.get_note(args.id) or db.find_note_by_prefix(args.id)
    if not note:
        print(f"Not found: {args.id}")
        sys.exit(1)
    print(f"Title:   {note['title']}")
    print(f"Project: {note['project'] or '-'}")
    print(f"Tags:    {note['tags']}")
    print(f"Created: {note['created_at']}")
    print(f"ID:      {note['id']}")
    print("-" * 60)
    print(note["content"])


def cmd_dump(args):
    print("=== PROJECTS ===")
    for name in db.list_projects():
        proj = db.get_project(name)
        print(f"  {name}")
        print(f"    {proj['overview'][:80]}")
        print(f"    updated: {proj['updated_at']}")
        print()

    rows = db.list_notes(project=args.project)
    print(f"=== NOTES ({len(rows)}) ===")
    for r in rows:
        tag_str = "  " + " ".join(r["tags"]) if r["tags"] else ""
        print(f"  [{r['project'] or '—':20}]  {r['title']}{tag_str}")
        print(f"    id: {r['id']}  created: {r['created_at']}")
        print()

    if args.note:
        note = db.get_note(args.note) or db.find_note_by_prefix(args.note)
        if note:
            print("─" * 60)
            print(f"Title:   {note['title']}")
            print(f"Project: {note['project']}")
            print(f"Tags:    {note['tags']}")
            print(f"Created: {note['created_at']}")
            print("─" * 60)
            print(note["content"])
        else:
            print(f"Note not found: {args.note}")


def _confirm(prompt: str, force: bool) -> bool:
    if force:
        return True
    print(prompt, end=" ")
    return input().strip().lower() == "y"


def cmd_delete(args):
    selectors = [s for s in (args.id, args.project, args.tag) if s]
    if len(selectors) != 1:
        print("Pass exactly one of: ID, --project NAME, or --tag TAG")
        sys.exit(1)

    if args.id:
        note = db.get_note(args.id) or db.find_note_by_prefix(args.id)
        if not note:
            print(f"Not found: {args.id}")
            sys.exit(1)
        if not _confirm(f"Delete note '{note['title']}' ({note['id'][:8]})? Cannot be undone. (y/n)", args.force):
            print("Cancelled.")
            return
        db.delete_note(note["id"])
        print(f"Deleted: {note['id']}")

    elif args.project:
        matches = db.list_notes(project=args.project)
        if not matches:
            print(f"No notes in project '{args.project}'.")
            return
        if not _confirm(f"Delete {len(matches)} note(s) in project '{args.project}'? Cannot be undone. (y/n)", args.force):
            print("Cancelled.")
            return
        n = db.delete_notes_by_project(args.project)
        print(f"Deleted {n} note(s).")

    elif args.tag:
        matches = [n for n in db.list_notes() if args.tag in n["tags"]]
        if not matches:
            print(f"No notes tagged '{args.tag}'.")
            return
        if not _confirm(f"Delete {len(matches)} note(s) tagged '{args.tag}'? Cannot be undone. (y/n)", args.force):
            print("Cancelled.")
            return
        n = db.delete_notes_by_tag(args.tag)
        print(f"Deleted {n} note(s).")


def cmd_delete_project(args):
    proj = db.get_project(args.name)
    if not proj:
        print(f"Project '{args.name}' not found.")
        sys.exit(1)
    note_count = len(db.list_notes(project=args.name))
    what = f"project '{args.name}'" + (f" and its {note_count} note(s)" if args.with_notes else "")
    if note_count and not args.with_notes:
        print(f"Note: {note_count} note(s) reference this project and will be kept, unlinked "
              f"(re-run with --with-notes to delete them too).")
    if not _confirm(f"Delete {what}? Cannot be undone. (y/n)", args.force):
        print("Cancelled.")
        return
    db.delete_project(args.name, with_notes=args.with_notes)
    print(f"Deleted project: {args.name}")


def _export_markdown(data: dict) -> str:
    lines = [f"# evoctx export — {_now_str()}", ""]
    for proj in data["projects"]:
        lines.append(f"## Project: {proj['name']}")
        lines.append("")
        if proj["overview"]:
            lines += ["**Overview**", "", proj["overview"], ""]
        if proj["conventions"]:
            lines += ["**Conventions**", "", proj["conventions"], ""]
        if proj["open_questions"]:
            lines += ["**Open questions**", "", proj["open_questions"], ""]
    lines.append("## Notes")
    lines.append("")
    for note in data["notes"]:
        tags = " ".join(note["tags"])
        lines.append(f"### {note['title']}")
        lines.append(f"*{note['project'] or 'no project'} · {note['created_at']} · {tags}*")
        lines.append("")
        lines.append(note["content"])
        lines.append("")
    lines.append("## Sessions")
    lines.append("")
    for s in data["sessions"]:
        state = "open" if s["open"] else "closed"
        lines.append(f"- **{s['id'][:8]}** [{s['project'] or '—'}] {state} — {s.get('intent') or ''}")
        if s["summary"]:
            lines.append(f"  {s['summary']}")
    return "\n".join(lines) + "\n"


def _now_str() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def cmd_export(args):
    import json

    data = db.export_all()
    if args.format == "json":
        output = json.dumps(data, indent=2)
    else:
        output = _export_markdown(data)

    if args.path:
        Path(args.path).write_text(output, encoding="utf-8")
        print(f"Exported {len(data['notes'])} note(s), {len(data['projects'])} project(s), "
              f"{len(data['sessions'])} session(s) to {args.path}")
    else:
        print(output)


def cmd_sync(args):
    context_dir = Path(args.path) if args.path else Path.cwd() / "context"
    if not context_dir.exists():
        print(f"No context directory found at {context_dir}")
        print("Layout: global.md, templates/*.md, projects/*.md")
        sys.exit(1)

    updated = 0

    # global.md → project "global"
    global_file = context_dir / "global.md"
    if global_file.exists():
        conventions = global_file.read_text(encoding="utf-8")
        existing = db.get_project("global") or {}
        db.upsert_project(
            "global",
            overview=existing.get("overview", "Cross-project global conventions"),
            conventions=conventions,
            open_questions=existing.get("open_questions", ""),
        )
        print(f"  global")
        updated += 1

    # templates/*.md → _name
    templates_dir = context_dir / "templates"
    if templates_dir.exists():
        for f in sorted(templates_dir.glob("*.md")):
            name = _template_name(f.stem)
            conventions = f.read_text(encoding="utf-8")
            existing = db.get_project(name) or {}
            db.upsert_project(
                name,
                overview=existing.get("overview", f"Template: {f.stem}"),
                conventions=conventions,
                open_questions=existing.get("open_questions", ""),
            )
            print(f"  template: {name}")
            updated += 1

    # projects/*.md → project conventions
    projects_dir = context_dir / "projects"
    if projects_dir.exists():
        for f in sorted(projects_dir.glob("*.md")):
            name = f.stem
            conventions = f.read_text(encoding="utf-8")
            existing = db.get_project(name) or {}
            db.upsert_project(
                name,
                overview=existing.get("overview", ""),
                conventions=conventions,
                open_questions=existing.get("open_questions", ""),
            )
            print(f"  project:  {name}")
            updated += 1

    print(f"\nSynced {updated} entries from {context_dir}")


DEMO_PROJECT = {
    "demo-webapp": {
        "overview": (
            "Example project — a fictional web app used to demonstrate the context store. "
            "React frontend, REST API backend, PostgreSQL."
        ),
        "conventions": """\
## Layer order
routes (thin) → services (logic) → repo (DB queries) → models

## Rules
- API responses wrapped in { "success": bool, "data": ... }
- Frontend never calls fetch directly — always through query hooks
- Migrations are additive-only in production""",
        "open_questions": """\
- Should sessions be stored server-side or in a signed cookie?
- Rate limiting strategy not yet decided""",
    },
}

DEMO_NOTES = [
    {
        "title": "Decision: JWT in httpOnly cookie, not localStorage",
        "content": """\
Chose httpOnly cookie for the auth token.

Why: localStorage is readable by any injected script (XSS), cookies with
httpOnly + SameSite=Lax are not. CSRF risk is handled by the SameSite flag
plus a double-submit token on mutating endpoints.

Revisit if: we ever need the token in a non-browser client.""",
        "project": "demo-webapp",
        "tags": ["#decision", "#auth"],
    },
    {
        "title": "Gotcha: FTS5 queries choke on unescaped punctuation",
        "content": """\
Passing raw user input into an FTS5 MATCH clause raises errors on characters
like '-' and '"'. Fix: wrap each term in double quotes, or fall back to a
LIKE query when MATCH raises.

This is exactly what the store's own search does — see db.py.""",
        "project": "demo-webapp",
        "tags": ["#gotcha", "#sqlite"],
    },
]


def cmd_demo_seed(args):
    for name, data in DEMO_PROJECT.items():
        db.upsert_project(name, **data)
        print(f"  upsert project: {name}")

    if not args.force and db.search("*", limit=1):
        print("Store already has notes — skipping note seed (use --force to append anyway)")
    else:
        for note in DEMO_NOTES:
            note_id = db.add_note(**note, author_client="cli")
            print(f"  add note: {note['title'][:55]} ({note_id[:8]})")

    print(f"\nStore: {STORE_DIR / 'store.db'}")


# ── argparse ──────────────────────────────────────────────────────────────────

def cmd_help(args):
    print(__doc__.strip())


def cmd_version(args):
    from . import __version__
    print(f"evoctx {__version__}")


def cmd_serve(args):
    """Launch the MCP server (stdio). Not for humans — an AI client invokes
    this via its MCP config, generated by `evoctx install <client>`.

    Spawns `python -m evoctx.server` as a child and waits, instead of
    importing the server module in-process: cli.py's module-level Database
    handle and the server's would otherwise both open the same SQLite file
    in one process, which is exactly the double-open situation WAL mode
    exists to make survivable — better to just not create it.

    Deliberately subprocess.run(), not os.execv(): CPython's os.execv on
    Windows is broken whenever sys.executable contains a space (confirmed
    with a minimal repro outside this codebase entirely — 0.1.0 shipped
    with execv and broke the MCP connection for any install path with a
    space in it, which is most of them). subprocess.run builds the Windows
    command line via the standard library's own quoting and handles this
    correctly; stdio is inherited by default so the MCP stdio transport
    passes through the extra process layer transparently."""
    result = subprocess.run([sys.executable, "-m", "evoctx.server"])
    sys.exit(result.returncode)


def main():
    # Windows consoles often default to cp1252, which can't print em-dashes
    # or box-drawing chars that legitimately appear in note content.
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    # bare `evoctx` prints the full command reference instead of an error
    if len(sys.argv) == 1:
        cmd_help(None)
        return

    from . import __version__

    p = argparse.ArgumentParser(prog="evoctx")
    p.add_argument("--version", action="version", version=f"evoctx {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    # help
    sub.add_parser("help", aliases=["?"], help="Show all commands with usage examples")
    sub.add_parser("version", help="Show the installed version")

    # serve (what MCP clients actually invoke)
    sub.add_parser("serve", aliases=["mcp"], help="Launch the MCP server (stdio) — for AI clients, not humans")

    # ── MCP tool mirrors ──
    sub.add_parser("list-projects", help="List registered projects (MCP: list_projects)")

    gp = sub.add_parser("get-project", help="Full project view (MCP: get_project)")
    gp.add_argument("name")

    sc = sub.add_parser("search-context", aliases=["search"], help="Search notes (MCP: search_context)")
    sc.add_argument("query")
    sc.add_argument("--project", "-p", default=None)
    sc.add_argument("--limit", "-n", type=int, default=10)

    # write-note / add
    a = sub.add_parser("write-note", aliases=["add"], help="Add a new note (MCP: write_note)")
    a.add_argument("title")
    a.add_argument("--project", "-p", default=None)
    a.add_argument("--tags", "-t", default="", help="comma-separated: '#decision,#auth'")
    g = a.add_mutually_exclusive_group()
    g.add_argument("--file", "-f", help="Read content from file")
    g.add_argument("--text", help="Inline content string")

    # import-dir
    b = sub.add_parser("import-dir", help="Import all .md/.txt files from a folder")
    b.add_argument("path")
    b.add_argument("--project", "-p", default=None)
    b.add_argument("--tags", "-t", default="")

    # update-project  (also works for templates: update-project _fastapi --field ...)
    c = sub.add_parser("update-project", help="Update a project or template field")
    c.add_argument("name")
    c.add_argument("--field", required=True, choices=["overview", "conventions", "open_questions"])
    g2 = c.add_mutually_exclusive_group()
    g2.add_argument("--file", "-f")
    g2.add_argument("--text")

    # init
    d = sub.add_parser("init", help="Auto-detect project from a directory and register it")
    d.add_argument("--dir", default=None, help="Directory to scan (default: cwd)")
    d.add_argument("--name", default=None, help="Override project name")
    d.add_argument("--template", default=None, help="Stamp with template conventions (e.g. fastapi)")
    d.add_argument("--force", action="store_true", help="Overwrite if project already exists")

    # new-project
    e = sub.add_parser("new-project", help="Register a project manually")
    e.add_argument("name")
    e.add_argument("--overview", default="")
    e.add_argument("--conventions", default="")
    e.add_argument("--open-questions", dest="open_questions", default="")
    e.add_argument("--template", default=None, help="Stamp with template conventions")

    # new-template
    nt = sub.add_parser("new-template", help="Create a reusable conventions template")
    nt.add_argument("name", help="Template name (underscore prefix added automatically)")
    nt.add_argument("--overview", default="")
    nt.add_argument("--conventions", default="")
    nt.add_argument("--file", "-f", help="Read conventions from file")

    # templates
    sub.add_parser("templates", help="List available templates")

    # list
    sub.add_parser("list", help="List notes").add_argument("--project", "-p", default=None)

    # delete (human-only, no MCP equivalent — deletion is deliberately not AI-operable)
    de = sub.add_parser("delete", help="Delete note(s) by ID, --project, or --tag. No MCP equivalent.")
    de.add_argument("id", nargs="?", default=None, help="Note ID, prefix ok")
    de.add_argument("--project", "-p", default=None, help="Delete all notes in this project")
    de.add_argument("--tag", "-t", default=None, help="Delete all notes with this tag")
    de.add_argument("--force", "-y", action="store_true", help="Skip confirmation")

    dp = sub.add_parser("delete-project", help="Delete a project. Notes are kept unlinked unless --with-notes.")
    dp.add_argument("name")
    dp.add_argument("--with-notes", action="store_true", help="Also delete every note in this project")
    dp.add_argument("--force", "-y", action="store_true", help="Skip confirmation")

    # get-by-id / show
    sub.add_parser("get-by-id", aliases=["show"], help="Show full note by ID, prefix ok (MCP: get_by_id)").add_argument("id")

    # sessions
    ss = sub.add_parser("start-session", help="Register a work session (MCP: start_session)")
    ss.add_argument("--project", "-p", default=None)
    ss.add_argument("--intent", "-i", default=None, help="One line on what this session is for")

    es = sub.add_parser("end-session", help="Close a session with a summary (MCP: end_session)")
    es.add_argument("summary")
    es.add_argument("--id", default=None, help="Session ID (prefix ok); default: latest open")
    es.add_argument("--project", "-p", default=None, help="With no --id: latest open in this project")

    rs = sub.add_parser("recent-sessions", help="List recent work sessions (MCP: recent_sessions)")
    rs.add_argument("--project", "-p", default=None)
    rs.add_argument("--limit", "-n", type=int, default=10)

    # grants
    lg = sub.add_parser("list-grants", help="What the active grant allows (MCP: list_grants)")
    lg.add_argument("--client", default="cli", help="Evaluate as this client name")

    gr = sub.add_parser("grant", help="Manage grants: list / show NAME / activate NAME / deactivate")
    gr.add_argument("action", choices=["list", "show", "activate", "deactivate"])
    gr.add_argument("name", nargs="?", default=None)

    # install / doctor
    ins = sub.add_parser("install", help="Write MCP config for a client")
    ins.add_argument("client", help="claude-code | claude-desktop | cursor | windsurf | vscode | codex")
    ins.add_argument("--uvx", action="store_true",
                     help="Write 'uvx evoctx serve' instead of the local executable (needs PyPI)")
    ins.add_argument("--hooks", action="store_true",
                     help="claude-code only: also write SessionStart/Stop hooks into settings.json "
                          "(the one enforcement mechanism beyond convention — see README)")
    ins.add_argument("--dir", default=None,
                     help="claude-code only: project/workspace root to write .mcp.json into "
                          "(default: current directory)")

    sub.add_parser("doctor", help="Verify store, grants, server command, client configs")

    # hook (internal — invoked by Claude Code, not humans)
    hk = sub.add_parser("hook", help="Internal: Claude Code hook entry points")
    hk.add_argument("event", choices=["session-start", "stop"])

    # audit
    au = sub.add_parser("audit", help="Query the audit log (human-only, newest first)")
    au.add_argument("--client", default=None)
    au.add_argument("--tool", default=None)
    au.add_argument("--action", choices=["read", "write", "denied", "error"], default=None)
    au.add_argument("--since", default=None, help="e.g. 30m, 24h, 7d")
    au.add_argument("--limit", "-n", type=int, default=50)

    ap = sub.add_parser("audit-prune", help="Delete audit entries older than a cutoff (human-only)")
    ap.add_argument("--older-than", default="90d", help="e.g. 30d, 180d (units: m=minutes, h=hours, d=days) — default 90d")
    ap.add_argument("--force", "-y", action="store_true", help="Skip confirmation")

    # dump
    du = sub.add_parser("dump", help="Read-only dump of the whole store")
    du.add_argument("--project", "-p", default=None)
    du.add_argument("--note", default=None, help="Also print one full note (ID prefix ok)")

    # export
    ex = sub.add_parser("export", help="Export the whole store (projects, notes, sessions)")
    ex.add_argument("path", nargs="?", default=None, help="Write to this file (default: print to stdout)")
    ex.add_argument("--format", choices=["json", "markdown"], default="json")

    # sync
    sy = sub.add_parser("sync", help="Sync a markdown folder into the store (global.md, templates/, projects/)")
    sy.add_argument("path", nargs="?", default=None, help="Folder to sync (default: ./context)")

    # demo-seed
    ds = sub.add_parser("demo-seed", help="Populate sample data to try the store out")
    ds.add_argument("--force", action="store_true", help="Append demo notes even if store has data")

    args = p.parse_args()
    {
        "help": cmd_help,
        "?": cmd_help,
        "version": cmd_version,
        "serve": cmd_serve,
        "mcp": cmd_serve,
        # MCP tool mirrors (+ short aliases)
        "list-projects": cmd_list_projects,
        "get-project": cmd_get_project,
        "search-context": cmd_search_context,
        "search": cmd_search_context,
        "get-by-id": cmd_show,
        "show": cmd_show,
        "write-note": cmd_add,
        "add": cmd_add,
        "start-session": cmd_start_session,
        "end-session": cmd_end_session,
        "recent-sessions": cmd_recent_sessions,
        "list-grants": cmd_list_grants,
        "grant": cmd_grant,
        "audit": cmd_audit,
        "audit-prune": cmd_audit_prune,
        "install": cmd_install,
        "doctor": cmd_doctor,
        "hook": cmd_hook,
        # store management
        "import-dir": cmd_import_dir,
        "update-project": cmd_update_project,
        "init": cmd_init,
        "new-project": cmd_new_project,
        "new-template": cmd_new_template,
        "templates": cmd_templates,
        "list": cmd_list,
        "delete": cmd_delete,
        "delete-project": cmd_delete_project,
        "dump": cmd_dump,
        "sync": cmd_sync,
        "demo-seed": cmd_demo_seed,
        "export": cmd_export,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
