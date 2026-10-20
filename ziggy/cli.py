"""Ziggy command line: new, run (retry), answer, approve, split, request-changes, fix, resume, stop, nudge, chat,
chat-stop, after, status, watch, log, open, publish, restack, review, prs, adopt, sync, cleanup, inbox, retro, memory, roadmap,
protected, lead, stats, route, target add, target suggest, project new, sessions, ui, listen and migrate."""
import argparse
import getpass
import json
import os
import plistlib
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import (attention, bmad, changes, chat, cleanup, dispatch, feedback, host, lead, memory, migrate, new_project, onboard,
               onboard_agent, onboard_verify, pipeline, project, progress, protected, publish, pulls, quota, retro, review, roadmap,
               sessions, stack, state, talk, ticket)
from .config import (MODES, PIPELINE_ROOT, ConfigError, load_host, load_lead, load_slack, load_target, parse_source,
                     source_settings, state_home, target_names)
from .worktree import GitError

SLACK_SDK = "slack_sdk==3.45.0"


def _read_text(path: str | None) -> str:
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8") if path else ""


SCISSORS = "# ------------------------ >8 ------------------------"
EDITOR_TEMPLATE = f"""

{SCISSORS}
# First line: [<type>:] <title>   types: {', '.join(ticket.TYPES)} (default feat)
#   e.g.  chore: remove langchain and langchain_community
# Following lines: the ticket body (markdown); write what the agent needs to know.
# Everything from the scissors line down is ignored. Save and close to create the
# ticket; leave the title empty to cancel.
"""


def _from_editor() -> str:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        editor = subprocess.run(["git", "var", "GIT_EDITOR"], capture_output=True, text=True).stdout.strip() or "vi"
    with tempfile.NamedTemporaryFile("w", suffix=".md", prefix="bmad-ticket-", delete=False, encoding="utf-8") as f:
        f.write(EDITOR_TEMPLATE)
    try:
        if subprocess.run(f'{editor} "{f.name}"', shell=True).returncode != 0:
            raise ValueError(f"editor `{editor}` failed; ticket not created")
        return Path(f.name).read_text(encoding="utf-8").split(SCISSORS)[0]
    finally:
        os.unlink(f.name)


def _cmd_new(target, args) -> int:
    if args.title is None and args.body_file is None:
        first, _, body = _from_editor().strip().partition("\n")
        if not first.strip():
            print("ziggy: empty title; no ticket created", file=sys.stderr)
            return 1
    else:
        first, body = args.title or "", _read_text(args.body_file)
    ticket_type, title = ticket.split_type(first, default=args.type or "feat")
    if args.type and ticket_type != args.type:
        raise ValueError(f"--type {args.type} and the title prefix `{ticket_type}:` disagree")
    origin = None
    if args.source:
        name, ref, _ = parse_source(args.source)
        origin = {"name": name, "ref": ref, "url": args.source_url}
    # A controlled target's code tickets run BMad when it is set up there; --pipeline standard keeps the type's.
    t = ticket.create(target, title, ticket_type, body, args.plan_checkpoint, args.risk, language=args.lang, origin=origin,
                      pipeline=args.pipeline or ticket.default_choice(target, ticket_type), after=args.after)
    print(f"{t['id']}\t{t['branch']}\t{target.name} ({target.path})")
    if not args.run:
        return 0
    return _finish(target, t["id"], pipeline.run(target, t["id"]))


def _summary(record: dict) -> str:
    cost = record.get("total_cost_usd")
    result = record.get("result") or {}
    verdict, route = record.get("verdict") or result.get("verdict"), record.get("route") or result.get("route")
    route = f" route={route}" if route else ""
    return (
        f"{record.get('stage') or record.get('phase')}: verdict={verdict or '-'}{route} "
        f"state={record.get('state') or '-'} model={record.get('model')} "
        f"cost=${f'{cost:.2f}' if isinstance(cost, (int, float)) else '?'} turns={record.get('num_turns')} "
        f"exit={record.get('exit_code')}"
        + (f" error={record['result_error']}" if record.get("result_error") else "")
        + (f"\n  record: {record['dir']}/run.json" if record.get("dir") else "")
    )


# Exit codes: 0 done, 3 waiting for a human (a question or the approval gate), 1 failed; errors are 2 (main).
# queued: recorded with --no-run; waiting: starts on its own once the tickets it waits for are done; split: its slices run;
# quota: waits for its Claude login's usage limit to reset, then continues on its own; host: waits for the machine to
# have memory again (host.py), then starts on its own.
EXIT = {"done": 0, "queued": 0, "needs-user": 3, "awaiting-approval": 3, stack.WAITING: 0, stack.SPLIT: 0, quota.STATUS: 0,
        host.STATUS: 0}


def _finish(target, ticket_id: str, records) -> int:
    for record in records if isinstance(records, list) else []:
        print(_summary(record))
    st = state.load_state(target, ticket_id)
    print(f"{ticket_id}: {st['status']}" + (f" ({st['error']})" if st["status"] == "failed" and st.get("error") else ""))
    hint = _next_command(target, ticket_id, st)
    if hint:
        print(f"  next: {hint}")
    return EXIT.get(st["status"], 1)


def _cmd_run(target, args) -> int:
    if args.full_review:
        print(f"{args.id}: a new run (reviews read the whole branch)")
    pipeline.IGNORE_QUOTA = pipeline.IGNORE_HOST = args.now
    return _finish(target, args.id, pipeline.run(target, args.id, full_review=args.full_review))


def _cmd_recheck(target, args) -> int:
    """A failed ticket whose READY visit failed an engine check: run only the checks again, then route on."""
    return _finish(target, args.id, pipeline.recheck(target, args.id))


def _cmd_resume(target, args) -> int:
    return _finish(target, args.id, pipeline.resume(target, args.id))


def _cmd_answer(target, args) -> int:
    text = args.text if args.text is not None else _read_text(args.file)
    if not text.strip():
        raise ValueError("the answer is empty")
    result = pipeline.answer(target, args.id, text, by=args.by or getpass.getuser(), via=args.via, run_after=not args.no_run)
    return _finish(target, args.id, result)


def _cmd_approve(target, args) -> int:
    return _finish(target, args.id, pipeline.approve(target, args.id, run_after=not args.no_run))


def _cmd_split(target, args) -> int:
    """At the approval gate: one stacked ticket per slice of the plan instead of one PR; the first slice runs."""
    made = stack.split(target, args.id)
    for c in made:
        print(f"{c['id']}\t{c['slice']}\t{c['branch']} ← {c['base_branch']}\t{c['title']}")
    if args.no_run:
        return 0
    return _finish(target, made[0]["id"], pipeline.run(target, made[0]["id"]))


def _cmd_restack(target, args) -> int:
    """A slice whose PR below was squash-merged: rebase it onto the base; --push sends the result to its PR."""
    if args.push:
        print(stack.restack_push(target, args.id))
        return 0
    out = stack.restack(target, args.id)
    result = out["result"]
    print(f"{args.id}: rebased onto {out['onto']} ({result['from'][:7]} → {result['to'][:7]})")
    for line in result["commits"]:
        print(f"  {line}")
    if out["marked"]:
        print(f"  stacked on it, now to restack too: {', '.join(out['marked'])}")
    if out["needs_push"]:
        print(f"  next: ziggy restack {args.id} --push   (force-with-lease to its own branch, after you checked it)")
    return 0


def _cmd_after(target, args) -> int:
    """Set the tickets this one starts after (before it runs); none clears them."""
    after = stack.set_after(target, args.id, args.deps)
    print(f"{args.id}: starts after {', '.join(after)}" if after else f"{args.id}: no dependencies")
    return 0


def _cmd_request_changes(target, args) -> int:
    """Send the review page's draft comments (or, with --github, the PR feedback items picked) back to the agent
    as a new run."""
    by = args.by or getpass.getuser()
    if args.github:
        sub = feedback.prepare(target, args.id, args.github, by=by)
        print(f"{args.id}: {sub['heading']} ({sub['comments']} item(s))")
        pipeline.request_changes(target, args.id, sub["section_md"], heading=sub["heading"], comments=sub["comments"],
                                 source="github", round=sub["round"], run_after=False)
        feedback.mark_sent(target, args.id, sub["ids"], sub["round"], sub["section_md"])
        return _finish(target, args.id, [] if args.no_run else pipeline.run(target, args.id))
    sub = changes.submit(target, args.id, by=by)
    print(f"{args.id}: {sub['heading']} ({sub['comments']} comment(s))")
    result = pipeline.request_changes(target, args.id, sub["section_md"], heading=sub["heading"],
                                      comments=sub["comments"], source="user", run_after=not args.no_run,
                                      findings=sub.get("findings") or ())
    return _finish(target, args.id, result)


def _fix_section(plan_text: str, heading: str, findings: list[str]) -> str:
    first = pipeline.next_task_number(plan_text)
    tasks = "\n".join(f"- [ ] T{first + i:03d} Fix: " + text.strip().replace("\n", "\n  ")
                      for i, text in enumerate(findings))
    return (f"## {heading}\n\nFindings from a second (Codex) review of this branch. The user kept each one; "
            f"fix them and change only what a finding needs.\n\n{tasks}\n")


def _cmd_fix(target, args) -> int:
    """Codex review findings the user kept → one fix task each, in a new run."""
    text = args.text if args.text is not None else _read_text(args.file) if args.file else None
    if text is None:
        done = [r for r in review.reviews(target, args.id) if r["state"] == "ok" and (not args.review or r["id"] == args.review)]
        if not done:
            raise LookupError(f"{args.id} has no finished review" + (f" {args.review}" if args.review else ""))
        text = done[0]["text"]
    findings = [f["text"] for f in review.findings(text)] or ([text] if text.strip() else [])
    if not findings:
        raise ValueError("no findings to fix")
    st = state.load_state(target, args.id)
    n = 1 + sum(1 for r in st.get("user_reviews") or [] if r.get("source") == "codex")
    heading = f"Review (codex, round {n})"
    plan = pipeline.plan_path(target, args.id)
    section = _fix_section(plan.read_text(encoding="utf-8") if plan.is_file() else "", heading, findings)
    print(f"{args.id}: {heading} ({len(findings)} finding(s))")
    result = pipeline.request_changes(target, args.id, section, heading=heading, comments=len(findings),
                                      source="codex", run_after=not args.no_run)
    return _finish(target, args.id, result)


def _cmd_stop(target, args) -> int:
    where = dispatch.stop(target, args.id)
    print(f"{args.id}: stop requested for {where}; the run ends failed (retry the stage with: ziggy run {args.id}, "
          f"or change course with: ziggy answer {args.id} --text ...)")
    return 0


def _cmd_nudge(target, args) -> int:
    """Steer the running stage: it is interrupted and resumes its own session with the message (a stage
    that has not started yet gets it in its prompt)."""
    text = args.text if args.text is not None else _read_text(args.file or "-")
    if not text.strip():
        raise ValueError("no message text (pass --text, --file, or stdin)")
    out = pipeline.nudge(target, args.id, text, by=args.by or getpass.getuser(), via=args.via)
    print(f"{args.id}: {out['message']}")
    return 0


def _cmd_chat(target, args) -> int:
    """Ask about the ticket in a read-only Claude session; follow-ups continue the same conversation."""
    turn = chat.send(target, args.id, args.text, new_thread=args.new, model=args.model)
    if turn.get("state") == "ok":
        print(turn.get("answer") or "")
        return 0
    print(f"ziggy: no answer: {turn.get('error') or turn.get('state')}", file=sys.stderr)
    return 1


def _cmd_chat_stop(target, args) -> int:
    out = chat.stop(target, args.id)
    print(f"{args.id}: {out.get('message') or 'stopping the reply'}")
    return 0


def _cmd_talk_new(target, args) -> int:
    meta = talk.create(target, "write" if args.write else "read", args.title)
    print(meta["id"])
    return 0


def _cmd_talk_send(target, args) -> int:
    """One message in a Sohbet chat; the answer continues the chat's session."""
    turn = talk.send(target, args.id, args.text, model=args.model)
    if turn.get("state") == "ok":
        print(turn.get("answer") or "")
        return 0
    print(f"ziggy: no answer: {turn.get('error') or turn.get('state')}", file=sys.stderr)
    return 1


def _cmd_talk_stop(target, args) -> int:
    out = talk.stop(target, args.id)
    print(f"{args.id}: {out['message']}")
    return 0


def _cmd_talk_list(target, args) -> int:
    for c in talk.list_chats(target):
        live = " · yazıyor" if c["live"] else ""
        print(f"{c['id']}  {c['mode']:<5}  {c['turns']:>3} mesaj  {c['title'] or '(boş)'}{live}")
    return 0


def _cmd_talk_delete(target, args) -> int:
    talk.delete(target, args.id, force=args.force)
    print(f"{args.id}: deleted")
    return 0


def _cmd_route(target, args) -> int:
    """Propose a ticket (type and title) for a request; --create makes it (after you agree)."""
    proposal = project.route(target, args.text)
    print(f"ticket: {proposal['type']}: {proposal['title']}\n  {proposal['reason']}")
    if not args.create:
        return 0
    t = ticket.create(target, proposal["title"], proposal["type"], args.text, False, "low", language=args.lang)
    print(f"{t['id']}\t{t['branch']}")
    return 0


def _cmd_retro(target, args) -> int:
    """Look back at a ticket now: one read-only session on the evidence no retro has read yet."""
    record = retro.run(target, args.id)
    if record["state"] == "skipped":
        print(f"{args.id}: {record['reason']}")
        return 0
    cost = record.get("total_cost_usd")
    print(f"{args.id}: retro {record['state']}" + (f" ${cost:.2f}" if isinstance(cost, (int, float)) else "")
          + f", {len(record['candidates'])} new memory candidate(s)"
          + (f"; dropped: {'; '.join(record['dropped'])}" if record.get("dropped") else "")
          + (f"\n  error: {record['result_error']}" if record.get("result_error") else ""))
    for c in memory.pending(target):
        if c["id"] in record["candidates"]:
            print(f"  {c['id']}\t{c['kind']}\t{c['title']}")
    return 0 if record["state"] == "ok" else 1


def _lead_line(p: dict) -> str:
    sources = ", ".join(f"{s['kind']} {s['title']}" for s in p.get("signals") or [])
    return (f"  {p['id']}\t{p['type']}\t{p['title']}" + (" (ajansız)" if p.get("agentless") else "")
            + (f"\n      why: {p['why']}" if p.get("why") else "") + (f"\n      from: {sources}" if sources else ""))


def _cmd_lead(args) -> int:
    """The lead: run it (today's, or --now), list its proposals, start one (personal targets) or reject one."""
    name = args.lead_target or args.target
    if args.action in ("start", "reject"):
        if not args.id:
            raise ValueError(f"lead {args.action} needs a proposal id (p<n>)")
        target = load_target(name)
        if args.action == "reject":
            p = lead.reject(target, args.id, args.reason)
            print(f"{target.name} {p['id']}: rejected; its signals are not proposed again")
            return 0
        t = lead.start(target, args.id, language=args.lang, run=not args.no_run)
        print(f"{t['id']}\t{t['branch']}\t{target.name} ({target.path})")
        return 0 if args.no_run else _finish(target, t["id"], pipeline.run(target, t["id"]))
    targets = [load_target(name)] if name else [load_target(n) for n in target_names()]
    if args.action == "run":
        cfg = load_lead()
        for target in targets:
            if not args.now and not lead.due(target, cfg):
                last = lead.last_runs().get(target.name) or {}
                why = ("[lead] enabled = false" if not cfg.enabled else f"already ran today ({last.get('started_at')})"
                       if last.get("day") == lead.now().date().isoformat() else f"starts at {cfg.at}")
                print(f"{target.name}: not now ({why}; --now runs it anyway)")
                continue
            out = lead.run_now(target, by="cli")
            print(f"{target.name}: {out['signals']} signal(s), {out['fresh']} new, {len(out['proposals'])} proposal(s)"
                  + (f" ajansız ({out['why']})" if out["agentless"] else "") + (f"; {out['why']}" if out["state"] == "skipped" else ""))
            for warning in out["warnings"]:
                print(f"  uyarı: {warning}")
    for target in targets:
        waiting = lead.pending(target)
        print(f"# {target.name} ({target.mode}): {len(waiting)} pending proposal(s)")
        for p in waiting:
            print(_lead_line(p))
    return 0


def _cmd_stats(args) -> int:
    """Cost and delivery across every target (the panel's İstatistik)."""
    from .dashboard import stats
    data = stats()
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
        return 0
    t = data["totals"]
    print(f"cost: today ${t['today']:.2f} · 7 days ${t['week']:.2f} · 30 days ${t['month']:.2f} · all ${t['all']:.2f} "
          f"({t['sessions']} sessions, {t['tickets']} tickets)")
    for g in data["by_target"]:
        print(f"  {g['name']}\t${g['cost']:.2f}\t{g['sessions']} sessions")
    d = data["delivery"]
    print(f"merged PRs, last {d['days']} days:")
    for row in [*d["by_target"], d["overall"]]:
        if not row["merged"]:
            print(f"  {row['name']}\tnone")
            continue
        per = f"${row['cost_per_pr']:.2f}" + (" of quota (subscription, API-priced)" if row["unit"] == "kota" else "")
        print(f"  {row['name']}\t{row['merged']} merged · {round(row['clean_share'] * 100)}% without change requests · "
              f"{row['rounds_per_pr']:.2f} change rounds per PR · {per} per PR")
    return 0


def _print_candidate(c: dict) -> None:
    sources = ", ".join(f"{s.get('ticket')} {s.get('ref')}" for s in c.get("sources") or [])
    print(f"{c['id']}\t{c['kind']}\t{c['title']}")
    for name, value in c["fields"].items():
        print(f"\t{name}: {value}")
    scope = memory.scope_line(c.get("scope") or {})
    if scope:
        print(f"\t{memory.SCOPE}: {scope}")
    print(f"\tkaynak: {sources or '-'}" + (f"\n\tbenzer: {', '.join(c['similar'])} (--replace: yerine geçsin)" if c.get("similar") else ""))


def _cmd_memory(target, args) -> int:
    """The target's project memory: its entries (list), the candidates waiting (candidates), or a decision."""
    action = args.action
    if action in ("approve", "reject", "forget") and not args.id:
        raise ValueError(f"memory {action} needs an id ({'M-<n>' if action == 'forget' else 'c<n>'})")
    if action == "list":
        view = memory.view(target)
        print(f"{target.name}: {view['dir']} ({view['source']})" + (f"\n  {view['hint']}" if view["hint"] else ""))
        for warning in view["warnings"]:
            print(f"  uyarı: {warning}")
        for kind in memory.KINDS:
            for e in view["entries"][kind]:
                status = {"replaced": f"yerini aldı {e['replaced_by'] or ''}".strip(), "forgotten": "unutuldu"}.get(e["status"], "")
                marks = ", ".join(x for x in (status, "soluk" if e["stale"] else "", f"{e['uses']} kez kullanıldı" if e["uses"] else "") if x)
                print(f"{e['id']}\t{kind}\t{e['title']}" + (f"\t({marks})" if marks else ""))
        if view["pending"]:
            print(f"{len(view['pending'])} candidate(s) waiting: ziggy memory candidates")
        return 0
    if action == "candidates":
        waiting = memory.pending(target)
        if not waiting:
            print(f"{target.name}: no memory candidates waiting")
        for c in waiting:
            _print_candidate(c)
        return 0
    if action == "forget":
        out = memory.forget(target, args.id)
        print(f"{out['id']}: unutuldu ({out['file']})")
        return 0
    c = memory.decide(target, args.id, action, text=args.text, title=args.title, replace=args.replace)
    where = roadmap.memory_path(target) if c["kind"] == "yol" else memory.location(target).dir / memory.FILES[c["kind"]]
    print(f"{c['id']}: {c['status']}" + (f" → {c['entry']} in {where}" if c["status"] == "approved" else "")
          + (f"; {', '.join(c['replaced'])} yerini aldı" if c.get("replaced") else ""))
    return 0


def _cmd_protected(target, args) -> int:
    """The ticket's patches of protected files (AGENTS.md, …): list them with their diffs, apply one (a commit on the
    ticket branch) or reject it."""
    if args.action in ("apply", "reject") and args.n is None:
        raise ValueError(f"protected {args.action} needs the patch number (see: ziggy protected {args.id})")
    if args.action == "apply":
        e = protected.apply(target, args.id, args.n, by=getpass.getuser())
        print(f"{args.id}: #{e['n']} applied as {e['sha'][:10]} ({', '.join(e['files'])})")
        return 0
    if args.action == "reject":
        e = protected.reject(target, args.id, args.n, args.reason or "", by=getpass.getuser())
        print(f"{args.id}: #{e['n']} rejected ({', '.join(e['files'])})")
        return 0
    entries = protected.listing(target, args.id)
    if not entries:
        print(f"{args.id}: no protected file changes")
    for e in entries:
        extra = {"applied": f" {(e.get('sha') or '')[:10]}", "rejected": f": {e['reason']}" if e.get("reason") else "",
                 "pending": f" (replaces applied #{', #'.join(map(str, e['replaces']))})" if e.get("replaces") else ""
                 }.get(e["status"], "")
        if e.get("superseded_by"):
            extra += f" (superseded by #{e['superseded_by']})"
        print(f"#{e['n']}\t{e['status']}{extra}\t{', '.join(e['files'])}\t({e.get('stage') or '?'}, {e.get('created_at') or '?'})")
        if e.get("summary"):
            print(f"  {e['summary']}")
        if e["status"] == "pending":
            print("\n".join("  | " + line for line in e["diff"].rstrip("\n").splitlines()))
    return 0


def _cmd_roadmap(target, args) -> int:
    """The target's roadmap: show it, write a draft (a read-only session; the panel's job runs this), or link a
    ticket to an item."""
    if args.action == "link":
        if not args.item or not args.ticket:
            raise ValueError("roadmap link needs an item and a ticket: ziggy roadmap link R-3 local-7")
        roadmap.link(target, args.item, args.ticket)
        print(f"{roadmap.normalize_item(args.item)} → {args.ticket}")
        return 0
    if args.action == "draft":
        record = roadmap.run_draft(target)
        cost = record.get("total_cost_usd")
        print(f"{target.name}: roadmap draft {record['state']}" + (f" ${cost:.2f}" if isinstance(cost, (int, float)) else "")
              + (f"\n  error: {record['result_error']}" if record.get("result_error") else ""))
        if record.get("draft"):
            print(record["draft"], end="")
        return 0 if record.get("draft") else 1
    data = roadmap.load(target)
    if data["source"] is None:
        print(f"{target.name}: no roadmap yet ({roadmap.memory_path(target)}); `ziggy roadmap draft` proposes one")
        return 0
    print(f"{target.name}: {data['source']} · {data['path']}" + (f"\n  ({data['shadowed']} is not used: BMad's epics win)" if data.get("shadowed") else ""))
    for warning in data["warnings"]:
        print(f"  uyarı: {warning}")
    for line in roadmap.cli_lines(data):
        print(line)
    return 0


def _cmd_publish(target, args) -> int:
    body = _read_text(args.body_file) if args.body_file else None
    print(publish.publish(target, args.id, foreign_ok=args.foreign_ok, title=args.title, body=body))
    return 0


def _cmd_prs(target, args) -> int:
    """The target's open PRs and the ticket that follows each."""
    items = pulls.listing(target, fresh=True)
    if not items:
        print(f"{target.name}: no open pull requests")
    for pr in items:
        flags = ", ".join(f for f, on in (("draft", pr["draft"]), ("fork", pr["fork"]), ("yours", pr["mine"])) if on)
        print(f"#{pr['number']}\t{pr['ticket'] or '-'}\t{pr['author']}\t{pr['head']} → {pr['base']}\t{pr['title']}" + (f"  ({flags})" if flags else ""))
    return 0


def _cmd_adopt(target, args) -> int:
    t = pulls.adopt(target, args.number)
    st = state.load_state(target, t["id"])
    print(f"{t['id']}\t{t['branch']}\t{st['pr']}")
    if args.review:
        args.id, args.post = t["id"], None
        return _cmd_review(target, args)
    return 0


def _cmd_listen(target, args) -> int:
    try:
        from .slack_socket import listen
    except ImportError:
        if os.environ.get("ZIGGY_REEXEC"):
            raise
        # slack_sdk is the one third-party dependency, and only listen needs it.
        os.environ["ZIGGY_REEXEC"] = "1"
        os.execvp("uv", ["uv", "run", "--no-project", "--python", "3.12", "--with", SLACK_SDK, "python", *sys.argv])
    listen(target, args.slack)
    return 0


def _config_targets(current) -> list:
    """Every configured target that loads (the quota lines name each Claude login once), the current one at least."""
    out = []
    for name in target_names():
        try:
            out.append(current if name == current.name else load_target(name))
        except ConfigError:
            continue
    return out or [current]


def _cost(target, ticket_id: str) -> float:
    total = 0.0
    for d in [*state.phase_dirs(target, ticket_id), *retro.session_dirs(target, ticket_id)]:
        try:
            cost = state.read_json(d / "run.json").get("total_cost_usd")
        except (OSError, ValueError):
            continue
        total += cost if isinstance(cost, (int, float)) else 0
    return total


BAR = {"done": "✓", "current": "●", "pending": "○", "skipped": "–", "failed": "✗"}


def _bar(target, ticket_id: str) -> str:
    return " → ".join(f"{s['id']} {BAR.get(s['state'], '?')}" + (f"×{s['visits']}" if (s.get("visits") or 0) > 1 else "")
                      for s in pipeline.stages_view(target, ticket_id))


def _next_command(target, ticket_id: str, st: dict) -> str | None:
    """What to type next, by status."""
    if state.running(target, ticket_id):
        return f"ziggy watch {ticket_id}   (steer it without starting over: ziggy nudge {ticket_id} --text '...')"
    status = st["status"]
    if st.get("restack"):
        if st["restack"].get("rebased"):
            return f"ziggy restack {ticket_id} --push   (the rebased branch to its PR, force-with-lease)"
        return f"ziggy restack {ticket_id}   (rebase onto {st['restack'].get('onto')}; pushing is a separate step)"
    if status == "needs-user":
        return f"ziggy answer {ticket_id} --text '...'"
    if status == "awaiting-approval":
        split = (f" or ziggy split {ticket_id} (one stacked PR per slice)"
                 if stack.split_offer(target, ticket_id, st=st) else "")
        return f"ziggy approve {ticket_id}{split}   (or change course: ziggy answer {ticket_id} --text '...')"
    if status != "failed" and protected.pending(st):
        return f"ziggy protected {ticket_id}   (a protected file change waits: apply <n> or reject <n>)"
    if status == "failed":
        resume = f"ziggy resume {ticket_id} or " if pipeline.resumable(target, ticket_id) else ""
        return f"{resume}ziggy run {ticket_id}"
    if status == "done" and st.get("worktree") and not st.get("archived"):
        return (f"review in the panel, then ziggy request-changes {ticket_id}, "
                f"or ziggy publish {ticket_id} for a draft PR")
    if status == "new":
        return f"ziggy run {ticket_id}"
    if status == stack.WAITING:
        deps = st.get("waiting_for") or []
        said = f"{', '.join(deps or ['its dependencies'])} {'is' if len(deps) == 1 else 'are'} done"
        if target.mode == "controlled":  # a controlled target never starts a dependant by itself
            if not stack.unmet(target, state.load_ticket(target, ticket_id)):
                return f"ziggy run {ticket_id}   (its dependencies are done; a controlled target asks before starting it)"
            return f"nothing yet: when {said}, ziggy run {ticket_id} starts it (a controlled target does not start it by itself)"
        return f"nothing: it starts on its own when {said}"
    if status == quota.STATUS:
        q = st.get("quota") or {}
        return (f"nothing: it {'continues' if q.get('reason') == 'rejected' else 'starts'} on its own at "
                f"{quota.clock(quota.epoch_of(q.get('resume_at')))} (ziggy run {ticket_id} --now starts it anyway)")
    if status == host.STATUS:
        return (f"nothing: it starts on its own once the machine has memory again (two calm panel ticks; "
                f"ziggy run {ticket_id} --now starts it anyway)")
    if status == stack.SPLIT:
        return f"ziggy status <slice> for each of {', '.join(st.get('children') or [])}"
    return None


def _bmad_line(target) -> str:
    found = bmad.status(target)
    if not found["installed"]:
        return f"BMad not installed ({found['reason']})"
    missing = bmad.local_ready(target)
    version = f" {found['version']}" if found["version"] else ""
    return (f"BMad{version} installed"
            + (f"; per-developer files missing in the main checkout: {', '.join(missing)}" if missing
               else " (tickets may use --pipeline bmad)"))


def _cmd_status(target, args) -> int:
    ids = [args.id] if args.id else state.ticket_ids(target)
    if not args.id:
        print(f"# {target.name}: {_bmad_line(target)}")
        for row in quota.view(_config_targets(target)):
            print(f"# quota {row['label']} ({', '.join(row['targets'])}): {quota.text(row)}")
        print(f"# host: {host.cli_text(host.sample(), load_host())}")
    for ticket_id in ids:
        t = state.load_ticket(target, ticket_id)
        st = state.load_state(target, ticket_id)
        print(f"{ticket_id}\t{st['status']}\t{st.get('stage') or '-'}\t{t['branch']}\t${_cost(target, ticket_id):.2f}"
              f"\t{st.get('updated_at') or '-'}\t{t['title']}")
        if not args.id:
            continue
        print(f"  pipeline: {st.get('pipeline') or '-'}")
        if st.get("pipeline"):
            print(f"  stages: {_bar(target, ticket_id)}")
        tasks = st.get("tasks") or {}
        if tasks.get("total"):
            print(f"  tasks: {tasks.get('done', 0)}/{tasks['total']}")
        print(f"  risk: {st.get('risk') or '-'}  size: {st.get('size') or '-'}")
        print(f"  running: {'yes' if state.running(target, ticket_id) else 'no'}\n  worktree: {st.get('worktree') or '-'}")
        if st.get("pr"):
            print(f"  pr: {st['pr']}")
        if st["status"] == "failed" and st.get("error"):
            print(f"  error: {st['error']}")
        q = st.get("quota") if st["status"] == quota.STATUS else None
        if q:
            print(f"  --- held for the usage limit of {q.get('label')} ({'its session was rejected' if q.get('reason') == 'rejected' else 'not started'}"
                  f" at {q.get('stage')}); resumes at {quota.clock(quota.epoch_of(q.get('resume_at')))}")
        h = st.get("host") if st["status"] == host.STATUS else None
        if h:
            print(f"  --- held while the machine is short of memory (since {h.get('since')}, before {h.get('stage')}); "
                  "starts after two calm panel ticks")
        info = stack.view(target, t, st)
        if info["after"]:
            print("  after: " + ", ".join(f"{d['id']} ({d['status'] or 'missing'})" for d in info["after"]))
        if info["parent"]:
            print(f"  slice: {t['slice']} of {info['parent']['id']} · base {t['base_branch']}"
                  + (f" (the branch of {t['stack_base']})" if t.get("stack_base") else ""))
        for c in info["children"] if st["status"] == stack.SPLIT else []:
            print(f"  - {c['slice']} {c['id']}\t{c['status']}\t{c.get('pr') or c.get('branch')}")
        if st.get("restack"):
            r = st["restack"]
            print(f"  --- restack: onto {r.get('onto')} from {str(r.get('upstream'))[:7]} ({r.get('reason')})"
                  + (", rebased, not pushed yet" if r.get("rebased") else ""))
        gate = st.get("gate")
        if st["status"] == "awaiting-approval" and gate:
            print(f"  --- waiting for your approval after {gate.get('stage')}: {gate.get('reason') or '-'}")
        round_ = pipeline.open_round(st) if st["status"] == "needs-user" else None
        if round_:
            print(f"  --- questions from {round_.get('stage')} (round {round_.get('n')})")
            if round_.get("context"):
                print(f"  {round_['context']}")
            gap = round_.get("intent_gap")
            if gap:
                print(f"  intent gap: {gap.get('summary')}")
                if gap.get("evidence"):
                    print(f"  evidence: {gap['evidence']}")
                kept = "the branch was reset to " + (gap.get("reset_to") or "")[:10] if gap.get("reset") else \
                    f"the branch was not reset ({gap.get('kept')})"
                print(f"  attempt {gap.get('attempt')}: {state.run_dir(target, ticket_id) / gap['patch']}; {kept}")
            for i, q in enumerate(round_.get("questions") or [], 1):
                print(f"  {i}. {q.get('text')}")
                for option in q.get("options") or []:
                    mark = " (recommended)" if option == q.get("recommendation") else ""
                    print(f"     - {option}{mark}")
                if q.get("recommendation") and q["recommendation"] not in (q.get("options") or []):
                    print(f"     recommended: {q['recommendation']}")
        hint = _next_command(target, ticket_id, st)
        if hint:
            print(f"  next: {hint}")
    return 0


def _cmd_watch(target, args) -> int:
    progress.watch(target, args.id, brief=args.brief, show_all=args.all, out=lambda line: print(line, flush=True))
    return 0


def _cmd_log(target, args) -> int:
    dirs = state.phase_dirs(target, args.id)
    records = []
    for d in dirs:
        try:
            records.append({**state.read_json(d / "run.json"), "dir": str(d)})
        except (OSError, ValueError):
            continue
    if not records:
        print(f"{args.id}: no runs yet")
        return 0
    for record in records:
        print(_summary(record))
    if args.message:
        print("--- final message of the last session")
        print(records[-1].get("final_message", ""))
    return 0


def _cmd_open(target, args) -> int:
    if args.plan:
        path = pipeline.plan_path(target, args.id)
        if not path.is_file():
            print(f"{args.id}: no plan yet", file=sys.stderr)
            return 1
        print(path)
        return 0
    st = state.load_state(target, args.id)
    if not st.get("worktree"):
        print(f"{args.id}: no worktree yet", file=sys.stderr)
        return 1
    print(st["worktree"])
    return 0


def _cmd_sessions(args) -> int:
    sessions.print_listing([load_target(name) for name in target_names()], show_all=args.all, as_json=args.json)
    return 0


UI_LABEL = "local.ziggy.ui"


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{UI_LABEL}.plist"


def _launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["launchctl", *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise OSError(f"launchctl {' '.join(args)}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def _cmd_ui(args) -> int:
    domain = f"gui/{os.getuid()}"
    url = f"http://127.0.0.1:{args.port}/"
    if args.uninstall:
        _launchctl("bootout", f"{domain}/{UI_LABEL}", check=False)
        _plist_path().unlink(missing_ok=True)
        print(f"panel removed from launchd ({UI_LABEL})")
        return 0
    if args.install:
        _install_ui(args.port)
        return 0
    if args.open:
        subprocess.run(["open", url], check=False)
        return 0
    from .dashboard import serve
    serve(args.port)
    return 0


def _cmd_mcp(args) -> int:
    from .mcp_server import serve
    return serve(args.port, args.read_only)


def _install_ui(port: int) -> None:
    """The panel as a launchd service (starts at login, restarted when it exits)."""
    domain = f"gui/{os.getuid()}"
    logs = state_home()
    logs.mkdir(parents=True, exist_ok=True)
    # launchd starts with a bare environment: hand it the PATH that finds claude, gh, git, uv
    # and npm now, and the locations of the config and the state.
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "ZIGGY_CONFIG", "XDG_CONFIG_HOME", "XDG_STATE_HOME") if os.environ.get(k)}
    plist = {
        "Label": UI_LABEL,
        "ProgramArguments": [sys.executable, str(PIPELINE_ROOT / "bin" / "ziggy"), "ui", "--port", str(port)],
        "EnvironmentVariables": env,
        "WorkingDirectory": str(Path.home()),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        # Runs started from the panel live in their own sessions; a panel restart must not
        # take them down with it.
        "AbandonProcessGroup": True,
        "StandardOutPath": str(logs / "ui.out.log"),
        "StandardErrorPath": str(logs / "ui.err.log"),
    }
    path = _plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _launchctl("bootout", f"{domain}/{UI_LABEL}", check=False)
    with path.open("wb") as f:
        plistlib.dump(plist, f)
    _launchctl("bootstrap", domain, str(path))
    print(f"panel installed in launchd ({path}); http://127.0.0.1:{port}/")


def _cmd_migrate(args) -> int:
    return migrate.run(args.dry_run, _install_ui)


def _print_proposal(proposal: dict) -> None:
    """The proposal's lines with their evidence and verification badges (as the panel's form shows them)."""
    for kind in ("prepare", "env", "checks"):
        items = proposal.get(kind) or []
        print(f"{kind}:" + ("" if items else " (none)"))
        for item in items:
            text = item["run"] if kind == "prepare" else f"{item['key']}={item['value']}" if kind == "env" else f"{item['name']}: {item['run']}"
            source = " (ajan)" if item.get("source") == "agent" else ""
            suggested = " (öneri)" if item.get("suggested") else ""
            print(f"  [{onboard_verify.badge(item.get('verify'))}] {text}{source}{suggested}")
            if item.get("why"):
                print(f"       why: {item['why']}")
            left = (item.get("verify") or {}).get("left_files")
            if left:
                print(f"       left behind (removed): {', '.join(left)}")
    if proposal.get("check_sandbox"):
        print(f"check sandbox presets: {', '.join(proposal['check_sandbox'])}")
    if proposal.get("notes"):
        print("notes:")
        for note in proposal["notes"]:
            print(f"  - {note}")
    if proposal.get("agents_md"):
        print("AGENTS.md draft (open it as a draft PR from the panel's form):")
        print("\n".join("  | " + line for line in proposal["agents_md"].splitlines()))
    print()


def _cmd_target_add(args) -> int:
    found = onboard.detect(args.path)
    spec = found["spec"]
    if args.name:
        spec["name"] = args.name
        found = {**found, "checks": [c for c in found["checks"] if c["key"] != "name"], "can_add": True}
    if args.mode:
        spec["mode"] = args.mode
    proposal = found["proposal"]
    if args.agent or args.verify:
        out = onboard_agent.suggest(spec["path"], proposal, agent=args.agent, verify=args.verify, target_name=spec["name"],
                                    progress=lambda o: print(f"… {o['phase']} {o['message']}".rstrip(), file=sys.stderr))
        proposal = out["proposal"]
        spec.update(onboard.spec_from(proposal))
        if out["cost_usd"]:
            print(f"agent cost: ${out['cost_usd']:.2f}", file=sys.stderr)
        if out["error"]:
            print(f"ziggy: {out['error']}", file=sys.stderr)
        if out["summary"] and out["summary"].get("error"):
            print(f"ziggy: {out['summary']['error']}", file=sys.stderr)
    _print_proposal(proposal)
    print(onboard.render(spec))
    for c in found["checks"]:
        print(f"  {'ok ' if c['ok'] else ('!! ' if c['blocking'] else '-- ')} {c['message']}" + (f"\n       → {c['fix']}" if c["fix"] else ""))
    if not found["can_add"]:
        print("ziggy: fix the blocking check first (--name picks another name)", file=sys.stderr)
        return 1
    if not args.yes:
        answer = input(f"\nappend [targets.{spec['name']}] to the config? [y/N] ").strip().lower()
        if answer not in ("y", "yes", "e", "evet"):
            print("nothing written")
            return 1
    backup = onboard.add(spec)
    print(f"added {spec['name']} (backup of the previous config: {backup})")
    return 0


def _cmd_target_remove(args) -> int:
    from .dashboard import busy_tickets
    if not args.yes:
        answer = input(f"take [targets.{args.name}] out of the config? The repository and its tickets stay. [y/N] ").strip().lower()
        if answer not in ("y", "yes", "e", "evet"):
            print("nothing written")
            return 1
    backup = onboard.remove(args.name, busy_tickets)
    print(f"removed {args.name} (backup of the previous config: {backup})")
    return 0


def _mb(n: int) -> str:
    return f"{n / 1048576:.0f} MB" if n < 1 << 30 else f"{n / (1 << 30):.1f} GB"


def _cmd_target_leftovers(args) -> int:
    found = onboard.leftovers()
    for x in found:
        print(f"{x['name']}: {_mb(x['bytes'])} in {x['path']} ({len(x['worktrees'])} worktree(s), {_mb(x['worktree_bytes'])})")
        for t in x["worktrees"]:
            print(f"  {t['name']}  {_mb(t['bytes'])}{'  uncommitted changes' if t['dirty'] else ''}")
    if not found:
        print("no leftovers of removed targets")
    return 0


def _cmd_target_purge(args) -> int:
    out = onboard.purge(args.name, force=args.force)
    print(f"{args.name}: removed {', '.join(out['removed']) or 'nothing'} ({_mb(out['freed_bytes'])} freed)")
    return 0


def _cmd_target_suggest(args) -> int:
    out = onboard_agent.run_request(args.path, args.request, agent=args.agent)
    if out["error"]:
        print(f"ziggy: {out['error']}", file=sys.stderr)
        return 1
    _print_proposal(out["proposal"])
    return 0


def _cmd_source_map(args) -> int:
    name, source_project = args.name.lower(), args.project.upper()
    mapped = source_settings(name)["projects"].get(source_project)
    if not mapped:
        print(f"ziggy: no target for {name} project {source_project}; add it under [sources.{name}.projects] or pass --target", file=sys.stderr)
        return 1
    print(mapped)
    return 0


def _cmd_review(target, args) -> int:
    if args.post:
        print(f"posted review {args.post} to {review.post(target, args.id, args.post)}")
        return 0
    result = review.run(target, args.id)
    print(f"{args.id}: review of {(result['head'] or '')[:7]} against {result['base']}: {result['path']}")
    return 0


def _cmd_sync(target, args) -> int:
    results = cleanup.sync(target)
    for r in results:
        print(f"{r['id']}: " + (f"kept ({'; '.join(r['kept'])})" if "kept" in r else
                                f"base moved to {r['retargeted']}" + (", needs a restack (ziggy restack "
                                                                      f"{r['id']})" if r.get("restack") else "")
                                if "retargeted" in r else
                                "archived" + (", worktree removed" if "pr_state" in r else "") + (", local branch deleted" if r.get("branch_deleted") else "")))
    if not results:
        print(f"{target.name}: nothing merged or closed to archive")
    for r in feedback.sync(target):
        print(f"{r['id']}: " + (f"PR feedback not read ({r['error']})" if "error" in r else f"{r['new']} new PR feedback item(s)"))
    return 0


def _cmd_cleanup(target, args) -> int:
    if args.id:
        # One finished ticket's worktree: teardown first, the branch stays; a new run recreates it.
        print(cleanup.remove(target, args.id, force=args.force)["message"])
        return 0
    if args.force:
        raise ValueError("--force needs a ticket id")
    found = cleanup.candidates(target)
    if not found:
        print(f"{target.name}: no ticket worktrees with a PR")
        return 0
    for c in found:
        print(f"{c['id']}\t{c['pr_state'] or '?'}\t{'clean' if c['cleanable'] else 'keep: ' + '; '.join(c['reasons'])}\t{c['worktree']}")
    ready = [c for c in found if c["cleanable"]]
    if not ready:
        return 0
    if not args.yes:
        answer = input(f"remove {len(ready)} worktree(s) (and the local branch of merged PRs)? [y/N] ").strip().lower()
        if answer not in ("y", "yes", "e", "evet"):
            print("nothing removed")
            return 1
    for c in ready:
        done = cleanup.clean(target, c["id"])
        print(f"{done['id']}: worktree removed" + (", local branch deleted" if done["branch_deleted"] else ""))
    return 0


def _cmd_inbox(args) -> int:
    """What needs you, across every target (the panel's inbox); --seen marks it read."""
    targets = [load_target(name) for name in target_names()]
    attention.reconcile(targets)
    items = attention.items(targets, all=args.all)
    if args.json:
        print(json.dumps(items, indent=2, ensure_ascii=False))
    elif not items:
        print("nothing needs you")
    for item in [] if args.json else items:
        mark = "resolved" if item.get("resolved_at") or item.get("dismissed_at") else "seen" if item.get("seen_at") else "new"
        what = f"{item['ticket']}: {item['title']}"
        print(f"{mark}\t{item['kind']}\t{what[:70]}\t{_ago(item.get('created_at'))}\t{item['target']}/{item['key']}")
    if args.seen:
        changed = sum(attention.seen(t) for t in targets)
        print(f"{changed} marked seen", file=sys.stderr)
    return 0


def _ago(iso: str | None) -> str:
    """How long ago, short: 5m, 3h, 2d."""
    if not iso:
        return "-"
    seconds = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds())
    return f"{int(seconds // 60)}m" if seconds < 3600 else f"{int(seconds // 3600)}h" if seconds < 86400 else f"{int(seconds // 86400)}d"


def _cmd_project_new(args) -> int:
    parent = args.dir or str(Path.home() / "Developer")
    defaults = new_project.project_defaults(parent, args.name)
    owner = args.owner or args.gh_user or defaults["gh_user"]
    print(f"{Path(parent).expanduser() / args.name}: GitHub {owner}/{args.name} ({'public' if args.public else 'private'}), "
          f"Claude {defaults['claude_account'] or 'unknown'}")
    if not args.yes:
        answer = input("create it and push the first commit? [y/N] ").strip().lower()
        if answer not in ("y", "yes", "e", "evet"):
            print("nothing created")
            return 1
    name = new_project.create_project(args.name, parent, args.description or "", owner, private=not args.public,
                                     gh_user=args.gh_user, claude_config_dir=args.claude_config_dir)
    print(f"created target {name}")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ziggy", description=__doc__)
    p.add_argument("--target", help="target name from the config (default: default_target)")
    p.add_argument("--no-wait", action="store_true", help="when every run slot of the target is busy, fail instead of waiting in line")
    sub = p.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="create a local ticket (no arguments: write it in $EDITOR)")
    new.add_argument("title", nargs="?", help="[<type>:] <title>, e.g. 'chore: remove langchain'")
    new.add_argument("--type", choices=ticket.TYPES, help="ticket type when the title has no prefix (default feat)")
    new.add_argument("--body-file", help="ticket body (markdown); - reads stdin")
    new.add_argument("--run", action="store_true", help="run the ticket right after creating it")
    new.add_argument("--plan-checkpoint", action="store_true", help="always stop at the approval gate after planning")
    new.add_argument("--risk", default="low", choices=("low", "medium", "high"))
    new.add_argument("--source", help="the issue this ticket comes from, e.g. youtrack:PROJ-12 (names the ticket proj-12 and picks the target from [sources.<name>.projects])")
    new.add_argument("--source-url", help="link to that issue")
    new.add_argument("--pipeline", choices=ticket.CHOICES, help="standard (the type's pipeline) or bmad (BMad's "
                     "build-auto plans and builds; the target must have BMad installed). Default: bmad in a controlled "
                     "target with BMad installed and ready, else standard")
    new.add_argument("--lang", choices=tuple(ticket.LANGUAGES), help="language of the plan, notes and questions (tr or en)")
    new.add_argument("--after", action="append", default=[], metavar="ID",
                     help="start only after this ticket of the same target is done (repeatable); until then it waits")
    new.set_defaults(func=_cmd_new)

    run = sub.add_parser("run", aliases=["retry"], help="run the ticket's pipeline (a failed ticket retries the stage it failed in)")
    run.add_argument("id")
    run.add_argument("--full-review", action="store_true", help="a new run whose reviews read the whole branch, not only what changed since the last review")
    run.add_argument("--now", action="store_true", help="start even while the target's Claude login is over its usage limit "
                                                        "or the machine is short of memory (it warns)")
    run.set_defaults(func=_cmd_run)
    recheck = sub.add_parser("recheck", help="a failed ticket whose READY visit failed an engine check (its environment "
                                             "fixed since): run only the checks again, no agent, then continue")
    recheck.add_argument("id")
    recheck.set_defaults(func=_cmd_recheck)
    resume = sub.add_parser("resume", help="continue the last session where it ended (killed, crashed or timed out)")
    resume.add_argument("id")
    resume.set_defaults(func=_cmd_resume)

    answer = sub.add_parser("answer", help="answer the open questions (or change course after a stop), then continue")
    answer.add_argument("id")
    source = answer.add_mutually_exclusive_group(required=True)
    source.add_argument("--text")
    source.add_argument("--file", help="answer text (markdown); - reads stdin")
    answer.add_argument("--by", help="who answers (default: your user name)")
    answer.add_argument("--via", default="cli", choices=("cli", "panel", "slack", "skill"), help="where the answer came from (recorded)")
    answer.add_argument("--no-run", action="store_true", help="record only; continue later with `run`")
    answer.set_defaults(func=_cmd_answer)

    approve = sub.add_parser("approve", help="approve the plan at the approval gate and continue")
    approve.add_argument("id")
    approve.add_argument("--no-run", action="store_true", help="record only; continue later with `run`")
    approve.set_defaults(func=_cmd_approve)

    split = sub.add_parser("split", help="at the approval gate: one stacked ticket (and draft PR) per slice of the plan; "
                                         "the first slice runs")
    split.add_argument("id")
    split.add_argument("--no-run", action="store_true", help="create the slices only; run the first with `run`")
    split.set_defaults(func=_cmd_split)

    after = sub.add_parser("after", help="set the tickets this one starts after (before it runs); no ids clears them")
    after.add_argument("id")
    after.add_argument("deps", nargs="*", metavar="ID")
    after.set_defaults(func=_cmd_after)

    changes_ = sub.add_parser("request-changes", help="send the review page's draft comments (or picked PR feedback) back as a new run")
    changes_.add_argument("id")
    changes_.add_argument("--by", help="reviewer name (default: your user name)")
    changes_.add_argument("--github", nargs="+", metavar="ITEM_ID",
                          help="send these GitHub feedback items (runs/<id>/github/feedback.json) instead of the drafts")
    changes_.add_argument("--no-run", action="store_true", help="record only; continue later with `run`")
    changes_.set_defaults(func=_cmd_request_changes)

    fix = sub.add_parser("fix", help="fix the Codex review findings you kept, in a new run")
    fix.add_argument("id")
    fix.add_argument("--review", metavar="REVIEW_ID", help="the review to take findings from (default: the newest finished one)")
    kept = fix.add_mutually_exclusive_group()
    kept.add_argument("--text", help="the findings you kept (markdown list; each item becomes one fix task)")
    kept.add_argument("--file", help="the same from a file; - reads stdin")
    fix.add_argument("--no-run", action="store_true", help="record only; continue later with `run`")
    fix.set_defaults(func=_cmd_fix)

    route = sub.add_parser("route", help="propose a ticket (type and title) for a request")
    route.add_argument("text")
    route.add_argument("--create", action="store_true", help="create the proposed ticket")
    route.add_argument("--lang", choices=sorted(ticket.LANGUAGES), help="output language of the ticket")
    route.set_defaults(func=_cmd_route)

    watch = sub.add_parser("watch", help="follow a ticket's run as readable lines; ends with the run")
    watch.add_argument("id")
    watch.add_argument("--brief", action="store_true", help="milestones only (no reads, no subagent internals)")
    watch.add_argument("--all", action="store_true", help="replay every session of the ticket, not just the newest")
    watch.set_defaults(func=_cmd_watch)

    stop = sub.add_parser("stop", help="end the ticket's running session")
    stop.add_argument("id")
    stop.set_defaults(func=_cmd_stop)

    nudge = sub.add_parser("nudge", help="steer the running stage: interrupt it and resume its session with your message "
                                         "(a stage that has not started gets it in its prompt)")
    nudge.add_argument("id")
    said = nudge.add_mutually_exclusive_group()
    said.add_argument("--text")
    said.add_argument("--file", help="message text; - reads stdin (the default when neither is given)")
    nudge.add_argument("--by", help="who steers (default: your user name)")
    nudge.add_argument("--via", default="cli", choices=("cli", "panel", "slack", "skill"), help="where the message came from (recorded)")
    nudge.set_defaults(func=_cmd_nudge)

    chat_ = sub.add_parser("chat", help="ask about a ticket in a read-only Claude session (follow-ups continue the conversation)")
    chat_.add_argument("id")
    chat_.add_argument("--text", required=True, help="the question")
    chat_.add_argument("--new", action="store_true", help="start a new conversation instead of continuing the last one")
    chat_.add_argument("--model", choices=chat.MODELS, help="model for this answer (default: the target's chat model)")
    chat_.set_defaults(func=_cmd_chat)
    chat_stop = sub.add_parser("chat-stop", help="stop the reply being written in the ticket's chat")
    chat_stop.add_argument("id")
    chat_stop.set_defaults(func=_cmd_chat_stop)

    talk_ = sub.add_parser("talk", help="Sohbet: free chats about the target, not tied to a ticket (read-only, or in "
                                        "a worktree of their own)")
    talk_sub = talk_.add_subparsers(dest="talk_command", required=True)
    tnew = talk_sub.add_parser("new", help="start a chat; prints its id")
    tnew.add_argument("--write", action="store_true", help="give it its own worktree (branch chat/<id>) where it may edit")
    tnew.add_argument("--title")
    tnew.set_defaults(func=_cmd_talk_new)
    tsend = talk_sub.add_parser("send", help="send a message and wait for the answer")
    tsend.add_argument("id")
    tsend.add_argument("--text", required=True)
    tsend.add_argument("--model", choices=chat.MODELS)
    tsend.set_defaults(func=_cmd_talk_send)
    tstop = talk_sub.add_parser("stop", help="stop the reply being written")
    tstop.add_argument("id")
    tstop.set_defaults(func=_cmd_talk_stop)
    tlist = talk_sub.add_parser("list", help="the target's chats, most recent first")
    tlist.set_defaults(func=_cmd_talk_list)
    tdel = talk_sub.add_parser("delete", help="remove a chat (and a write chat's worktree and branch)")
    tdel.add_argument("id")
    tdel.add_argument("--force", action="store_true", help="also when its branch still holds commits or changes")
    tdel.set_defaults(func=_cmd_talk_delete)

    pub = sub.add_parser("publish", help="push a done ticket's branch and open a draft PR")
    pub.add_argument("id")
    pub.add_argument("--foreign-ok", action="store_true", help="an adopted PR opened by someone else: push to their branch anyway")
    pub.add_argument("--title", help="PR title, used word for word (default: from the ticket)")
    pub.add_argument("--body-file", help="PR description, used word for word (default: composed from the plan and the review); - reads stdin")
    pub.set_defaults(func=_cmd_publish)

    restack = sub.add_parser("restack", help="a slice whose PR below was squash-merged: rebase it onto its new base "
                                             "(--push: then force-with-lease it to its own PR)")
    restack.add_argument("id")
    restack.add_argument("--push", action="store_true", help="push the rebased branch to its PR (after you checked it)")
    restack.set_defaults(func=_cmd_restack)

    prs = sub.add_parser("prs", help="the target's open pull requests and the ticket following each")
    prs.set_defaults(func=_cmd_prs)
    adopt = sub.add_parser("adopt", help="make an open PR a ticket (pr-<n>) to review with Codex and fix")
    adopt.add_argument("number", type=int)
    adopt.add_argument("--review", action="store_true", help="start a Codex review right away")
    adopt.set_defaults(func=_cmd_adopt)

    listen = sub.add_parser("listen", help="serve the Slack channel (Socket Mode); runs until stopped")
    listen.set_defaults(func=_cmd_listen)

    status = sub.add_parser("status", help="list tickets, or show one")
    status.add_argument("id", nargs="?")
    status.set_defaults(func=_cmd_status)

    log = sub.add_parser("log", help="per-run records of a ticket")
    log.add_argument("id")
    log.add_argument("--message", action="store_true", help="also print the last run's final message")
    log.set_defaults(func=_cmd_log)

    open_ = sub.add_parser("open", help="print the ticket's worktree path (or plan path)")
    open_.add_argument("id")
    open_.add_argument("--plan", action="store_true")
    open_.set_defaults(func=_cmd_open)

    ui = sub.add_parser("ui", help="serve the web panel on 127.0.0.1 (or install it in launchd)")
    ui.add_argument("--port", type=int, default=8787)
    mode = ui.add_mutually_exclusive_group()
    mode.add_argument("--install", action="store_true", help="keep it running with launchd (starts at login)")
    mode.add_argument("--uninstall", action="store_true", help="remove it from launchd")
    mode.add_argument("--open", action="store_true", help="open the panel in the browser")
    ui.set_defaults(func=_cmd_ui, needs_target=False)

    mcp = sub.add_parser("mcp", help="MCP server on stdio: lets another Claude session read and drive Ziggy through the running panel")
    mcp.add_argument("--port", type=int, default=8787, help="the panel's port")
    mcp.add_argument("--read-only", action="store_true", help="offer only the tools that read")
    mcp.set_defaults(func=_cmd_mcp, needs_target=False)

    mig = sub.add_parser("migrate", help="one-time move of an install from before the rename (bmad-pipeline): directories, "
                         "paths in the state, worktrees, Claude Code sessions, the launchd panel")
    mig.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    mig.set_defaults(func=_cmd_migrate, needs_target=False)

    tgt = sub.add_parser("target", help="manage targets")
    tgt_sub = tgt.add_subparsers(dest="target_command", required=True)
    add = tgt_sub.add_parser("add", help="look at a repository and append its [targets.<name>] table to the config")
    add.add_argument("path")
    add.add_argument("--name", help="target name (default: the folder name)")
    add.add_argument("--mode", choices=MODES, help="controlled (plan approval always, dependants ask to start, "
                     "no GitHub replies) or personal (default: controlled for a company repository, else personal)")
    add.add_argument("--yes", action="store_true", help="append without asking")
    add.add_argument("--agent", action="store_true", help=f"let a read-only agent session fill the gaps (paid, at most ${onboard_agent.BUDGET_USD:g}); "
                                                          "verifies afterwards")
    add.add_argument("--verify", action="store_true", help="try prepare and the checks in a throwaway clone first")
    add.set_defaults(func=_cmd_target_add, needs_target=False)
    rm = tgt_sub.add_parser("remove", help="take a target's [targets.<name>] tables out of the config (its repository and state stay)")
    rm.add_argument("name")
    rm.add_argument("--yes", action="store_true", help="remove without asking")
    rm.set_defaults(func=_cmd_target_remove, needs_target=False)
    left = tgt_sub.add_parser("leftovers", help="what removed targets left in the state dir (size, worktrees)")
    left.set_defaults(func=_cmd_target_leftovers, needs_target=False)
    purge = tgt_sub.add_parser("purge", help="remove the worktrees a removed target left behind (its branches, tickets and memory stay)")
    purge.add_argument("name")
    purge.add_argument("--force", action="store_true", help="also worktrees with uncommitted changes")
    purge.set_defaults(func=_cmd_target_purge, needs_target=False)
    sug = tgt_sub.add_parser("suggest", help="the panel's onboarding job: agent (--agent) and verification of a request")
    sug.add_argument("path")
    sug.add_argument("--request", required=True, help="the onboarding request id (its input.json and result.json)")
    sug.add_argument("--agent", action="store_true")
    sug.set_defaults(func=_cmd_target_suggest, needs_target=False)

    rev = sub.add_parser("review", help="second review of a done ticket's branch by Codex (codex review)")
    rev.add_argument("id")
    rev.add_argument("--post", metavar="REVIEW_ID", help="add that review to the ticket's draft PR as a comment")
    rev.set_defaults(func=_cmd_review)

    source = sub.add_parser("source", help="work sources (issue trackers reached through Claude connectors)")
    source_sub = source.add_subparsers(dest="source_command", required=True)
    smap = source_sub.add_parser("map", help="the target a source project maps to, from [sources.<name>.projects]")
    smap.add_argument("name")
    smap.add_argument("project")
    smap.set_defaults(func=_cmd_source_map, needs_target=False)

    sync = sub.add_parser("sync", help="follow the tickets' PRs: archive and clean the merged or closed ones, read new PR feedback "
                                       "(the panel does this every few minutes)")
    sync.set_defaults(func=_cmd_sync)

    clean = sub.add_parser("cleanup", help="no id: remove the worktrees of tickets whose PR is merged or closed; with an id: remove a "
                                           "finished ticket's worktree (teardown first; the branch stays; --force also drops uncommitted files)")
    clean.add_argument("id", nargs="?")
    clean.add_argument("--force", action="store_true", help="with an id: remove even with uncommitted files or a failing teardown")
    clean.add_argument("--yes", action="store_true", help="remove without asking")
    clean.set_defaults(func=_cmd_cleanup)

    inbox = sub.add_parser("inbox", help="what needs you across every target (the panel's inbox)")
    inbox.add_argument("--all", action="store_true", help="also items resolved or closed in the last 24 h")
    inbox.add_argument("--seen", action="store_true", help="mark every listed item as read")
    inbox.add_argument("--json", action="store_true", help="machine-readable output")
    inbox.set_defaults(func=_cmd_inbox, needs_target=False)

    lead_ = sub.add_parser("lead", help="the morning lead: what to work on next, as proposals (run: today's, once a day "
                                        "from [lead] at; --now: at once)")
    lead_.add_argument("action", nargs="?", default="run", choices=("run", "list", "start", "reject"))
    lead_.add_argument("id", nargs="?", help="proposal id (p<n>) for start/reject")
    lead_.add_argument("--target", dest="lead_target", help="one target (default: every target; start/reject: the usual choice)")
    lead_.add_argument("--now", action="store_true", help="run now, whatever the schedule says")
    lead_.add_argument("--reason", help="with reject: why (kept with the proposal)")
    lead_.add_argument("--lang", choices=sorted(ticket.LANGUAGES), help="with start: the ticket's output language")
    lead_.add_argument("--no-run", action="store_true", help="with start: create the ticket only (the only way in a controlled target)")
    lead_.set_defaults(func=_cmd_lead, needs_target=False)
    stats_ = sub.add_parser("stats", help="cost and delivery (merged PRs, change rounds, quota per PR) across every target")
    stats_.add_argument("--json", action="store_true", help="machine-readable output")
    stats_.set_defaults(func=_cmd_stats, needs_target=False)

    retro_ = sub.add_parser("retro", help="look back at a ticket now: propose project memory from what went wrong "
                                          "(read-only session, at most $1; runs on its own when a ticket is done)")
    retro_.add_argument("id")
    retro_.set_defaults(func=_cmd_retro)
    mem = sub.add_parser("memory", help="the target's project memory: entries, candidates to approve or reject, forget an entry")
    mem.add_argument("action", nargs="?", default="list", choices=("list", "candidates", "approve", "reject", "forget"))
    mem.add_argument("id", nargs="?", help="candidate id (c<n>) for approve/reject, entry id (M-<n>) for forget")
    mem.add_argument("--text", help="with approve: the main text as you want it written (edit and approve)")
    mem.add_argument("--title", help="with approve: the title as you want it written")
    mem.add_argument("--replace", action="store_true", help="with approve: the similar entries get 'Durum: yerini aldı'")
    mem.add_argument("--target", default=argparse.SUPPRESS, help="the target (also accepted before the command)")
    mem.set_defaults(func=_cmd_memory)
    prot = sub.add_parser("protected", help="changes to protected files (AGENTS.md, …) the agents prepared as patches: "
                                            "list them, apply one on the ticket branch (a commit), or reject it")
    prot.add_argument("id")
    prot.add_argument("action", nargs="?", default="list", choices=("list", "apply", "reject"))
    prot.add_argument("n", nargs="?", type=int, help="the patch number (#n in the list) for apply/reject")
    prot.add_argument("--reason", help="with reject: why (kept with the patch)")
    prot.set_defaults(func=_cmd_protected)
    road = sub.add_parser("roadmap", help="the target's roadmap (memory roadmap.md or BMad's epics): show, draft one "
                                          "(read-only session, at most $1), or link a ticket to an item")
    road.add_argument("action", nargs="?", default="show", choices=("show", "draft", "link"))
    road.add_argument("item", nargs="?", help="with link: the item (R-3, or a BMad story key such as 1-2)")
    road.add_argument("ticket", nargs="?", help="with link: the ticket that works on it (local-7)")
    road.add_argument("--target", default=argparse.SUPPRESS, help="the target (also accepted before the command)")
    road.set_defaults(func=_cmd_roadmap)

    project = sub.add_parser("project", help="start new projects")
    project_sub = project.add_subparsers(dest="project_command", required=True)
    pnew = project_sub.add_parser("new", help="new repository with README, AGENTS.md and docs/PROJECT.md, pushed to GitHub and added as a target")
    pnew.add_argument("name")
    pnew.add_argument("--dir", help="parent folder (default ~/Developer)")
    pnew.add_argument("--description", help="one line: what the project is for")
    pnew.add_argument("--owner", help="GitHub owner (default: the folder's gh account)")
    pnew.add_argument("--public", action="store_true", help="public repository (default private)")
    pnew.add_argument("--gh-user", help="gh account that creates the repository and opens PRs (default: the folder's)")
    pnew.add_argument("--claude-config-dir", help="Claude login the runs use ('' for the default login; default: the folder's)")
    pnew.add_argument("--yes", action="store_true", help="create without asking")
    pnew.set_defaults(func=_cmd_project_new, needs_target=False)

    sess = sub.add_parser("sessions", help="other Claude Code sessions on this machine (read-only)")
    sess.add_argument("--all", action="store_true", help="list every ended session in the window, not only the newest 10")
    sess.add_argument("--json", action="store_true", help="machine-readable output")
    sess.set_defaults(func=_cmd_sessions, needs_target=False)
    return p


def _inside_target() -> bool:
    try:
        from .config import _load, _target_for_cwd
        return _target_for_cwd(_load()[1].get("targets") or {}) is not None
    except ConfigError:
        return False


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    pipeline.WAIT_FOR_SLOT = not args.no_wait
    try:
        if not getattr(args, "needs_target", True):
            return args.func(args)
        name = args.target
        if args.command == "new" and args.source and not name and not _inside_target():
            # An issue goes to the repository its project maps to, unless --target says otherwise.
            src, _, source_project = parse_source(args.source)
            name = source_settings(src)["projects"].get(source_project)
        if args.command == "listen":
            args.slack = load_slack()
            name = name or args.slack.target
        target = load_target(name)
        for warning in target.warnings:
            print(f"uyarı: {warning}", file=sys.stderr)
        return args.func(target, args)
    except pipeline.LeftLine as exc:  # stopped while waiting for a run slot: nothing ran, nothing failed
        print(f"ziggy: {exc}", file=sys.stderr)
        return 0
    except (ConfigError, GitError, pipeline.PipelineError, protected.ProtectedError, publish.PublishError, new_project.SetupError, review.ReviewError, cleanup.CleanupError, project.RouteError, pulls.PullError, stack.StackError, migrate.MigrateError, state.LockError, lead.LeadRefused, LookupError, ValueError, OSError) as exc:
        print(f"ziggy: {exc}", file=sys.stderr)
        return 2
