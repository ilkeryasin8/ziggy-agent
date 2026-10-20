"""The project memory: where it lives (vault mapping, override, state), the markdown files (tolerant parsing, frontmatter,
append-only writes, the Durum line), candidates from question rounds and their approval, episodes, recall into the
stage prompts, usage and staleness, the migration of the old context.md and rule candidates, and secret scrubbing."""
import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from conftest import calls, cli, script

from ziggy import attention, config, memory, pipelines, state


def target():
    return config.load_target("demo")


def mem_dir(env) -> Path:
    return env["state"] / "memory"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def vault(env, tmp_path, roots):
    v = tmp_path / "Vault"
    write(v / ".claude" / "scripts" / "projects.json", json.dumps({"version": 1, "roots": roots}))
    env["config"].write_text(env["config"].read_text() + f'[memory]\nvault = "{v}"\n')
    return v


def candidate(kind="karar", title="Use Postgres for reports", main="Postgres, not Mongo.", scope=None, ticket="local-1", **fields):
    return {"kind": kind, "title": title, "fields": {memory.MAIN[kind]: main, **fields}, "scope": scope or {},
            "sources": [{"ticket": ticket, "ref": "Q1-q1", "kind": "answer"}]}


class _Stage:
    def __init__(self, result):
        self.result = result


# --- location -----------------------------------------------------------------------------------------------------

def test_location_uses_an_exact_vault_mapping_only(env, tmp_path):
    assert memory.location(target()) == memory.Location(env["state"] / "memory", "state")
    v = vault(env, tmp_path, [
        {"prefix": str(env["repo"].parent), "project": "All/{leaf}", "vault_dir": "Projects/All"},
        {"prefix": str(env["repo"]), "project": "Demo", "vault_dir": "Projects/Demo"}])
    (v / "Projects" / "All").mkdir(parents=True)
    # The exact entry's folder does not exist yet: never created in the vault, the state is used with a hint.
    loc = memory.location(target())
    assert loc.source == "state" and loc.hint == "Vault'ta Projects/Demo klasörü yok (projects.json)"
    (v / "Projects" / "Demo").mkdir()
    loc = memory.location(target())
    assert loc == memory.Location(v / "Projects" / "Demo" / "memory", "vault", vault_dir="Projects/Demo")


def test_a_catch_all_parent_is_never_used(env, tmp_path):
    v = vault(env, tmp_path, [{"prefix": str(env["repo"].parent), "project": "All/{leaf}", "vault_dir": "Projects/All"},
                              {"prefix": "~/elsewhere", "project": "X", "vault_dir": "Projects/X"}])
    (v / "Projects" / "All").mkdir(parents=True)
    loc = memory.location(target())
    assert loc.dir == env["state"] / "memory" and loc.source == "state"
    assert loc.hint.startswith("Vault'ta bu repoya özel eşleme yok (projects.json)") and "Projects/All" in loc.hint
    assert "karıştıracağı" in loc.hint


def test_a_project_parent_gives_each_repo_its_own_subfolder(env, tmp_path):
    # One project folder holding two repos (e.g. a frontend and a backend): the nearest parent entry naming one
    # project wins over a catch-all, and each repo gets memory/<repo folder> so the two never mix.
    v = vault(env, tmp_path, [{"prefix": str(env["repo"].parent.parent), "project": "All/{leaf}", "vault_dir": "Projects/All"},
                              {"prefix": str(env["repo"].parent), "project": "Field", "vault_dir": "Projects/Field"}])
    loc = memory.location(target())
    assert loc.source == "state" and loc.hint.endswith("üst klasörün eşlemesindeki Projects/Field klasörü yok")
    (v / "Projects" / "Field").mkdir(parents=True)
    name = env["repo"].resolve().name
    loc = memory.location(target())
    assert loc == memory.Location(v / "Projects" / "Field" / "memory" / name, "vault", vault_dir=f"Projects/Field/memory/{name}")


def test_null_vault_dir_override_and_bad_config(env, tmp_path):
    vault(env, tmp_path, [{"prefix": str(env["repo"]), "project": "Demo", "vault_dir": None}])
    assert memory.location(target()).hint == "Vault'ta bu repoya özel eşleme yok (projects.json: vault_dir boş)"
    own = tmp_path / "own-memory"
    env["config"].write_text(env["config"].read_text().replace('gh_user = "tester"', f'gh_user = "tester"\nmemory_dir = "{own}"'))
    assert memory.location(target()) == memory.Location(own, "override")
    for bad, message in (('memory_dir = "relative/x"', "memory_dir must be an absolute path"),
                         ("memory_budget = 10", "memory_budget must be")):
        text = env["config"].read_text()
        env["config"].write_text(text.replace(f'memory_dir = "{own}"', bad))
        with pytest.raises(config.ConfigError, match=message):
            target()
        env["config"].write_text(text)
    env["config"].write_text(env["config"].read_text().replace("[memory]\n", "[memory]\nlimit = 3\n"))
    with pytest.raises(config.ConfigError, match=r"unknown keys in \[memory\]: limit"):
        config.load_memory()


def test_unreadable_projects_map_falls_back_with_a_hint(env, tmp_path):
    v = vault(env, tmp_path, [])
    (v / ".claude" / "scripts" / "projects.json").write_text("{not json")
    loc = memory.location(target())
    assert loc.source == "state" and loc.hint.startswith("Vault eşlemesi okunamadı (projects.json)")


# --- files ----------------------------------------------------------------------------------------------------------

USER_EDITED = """---
title: whatever the user made of it
modified: 2020-01-01
---
# My rules

Free text the user wrote.

## M-3 · kural · Regenerate the client
- Kural: Run `make schema`
  after changing models.
- NEDEN: drift
- ne zaman gecerli: dosyalar `api/models/**`, `api/client.py` · konular schema, codegen
- Kaynak: demo local-1 · R1-1 · 2026-10-01 · onay 2026-10-02
A note the user added.

## Notes of my own
- not an entry: kept

## m-4 | KURAL | Dates are UTC
- Kural: Store dates in UTC.
- Durum: Yerini aldı M-9

## M-x broken heading
## M-5 · karar · Wrong file
- Karar: kept anyway
"""


def test_parse_tolerates_user_edits_and_warns_without_failing(env):
    found, warnings = memory.parse(USER_EDITED, "kural", "rules.md")
    assert [e["id"] for e in found] == ["M-3", "M-4", "M-5"]
    m3, m4, m5 = found
    assert m3["title"] == "Regenerate the client" and m3["fields"]["Kural"] == "Run `make schema` after changing models."
    assert m3["fields"]["Neden"] == "drift" and m3["notes"] == ["A note the user added."]
    assert m3["scope"] == {"paths": ["api/models/**", "api/client.py"], "topics": ["schema", "codegen"]}
    assert (m3["created"], m3["approved"], m3["status"]) == ("2026-10-01", "2026-10-02", "active")
    assert m4["status"] == "replaced" and m4["replaced_by"] == "M-9" and m4["title"] == "Dates are UTC"
    assert m5["kind"] == "kural"
    assert warnings == ["rules.md:24: başlık okunamadı: M-x broken heading",
                        "rules.md:25: M-5 'karar' yazıyor ama rules.md kural dosyası; kural sayıldı"]
    write(mem_dir(env) / "rules.md", USER_EDITED)
    write(mem_dir(env) / "decisions.md", "## M-3 · karar · Duplicate id\n- Karar: x\n")
    found, warnings = memory.entries(target())
    assert [e["id"] for e in found] == ["M-3", "M-4", "M-5"]
    assert warnings[-1] == "decisions.md:1: M-3 rules.md:9 içinde de var; ilki kullanılıyor"
    [row] = [r for r in attention.items([target()]) if r["ticket"] == attention.MEMORY] or [None]
    assert row is None  # the panel loop raises it: reconcile
    attention.reconcile([target()])
    [row] = [r for r in attention.items([target()]) if r["ticket"] == attention.MEMORY]
    assert row["title"] == "Hafıza dosyasında 3 sorun" and row["key"].startswith("memory-warn:")


def test_approval_writes_frontmatter_appends_and_keeps_user_text(env):
    t = target()
    [c] = memory.add_candidates(t, [candidate("kural", "Run make schema", "Run `make schema` after model changes.",
                                              {"paths": ["api/models/**"], "topics": ["schema"]}, Neden="drift")], "retro", "local-1")
    out = memory.decide(t, c["id"], "approve", by="me")
    today = date.today().isoformat()
    assert out["status"] == "approved" and out["entry"] == "M-1" and out["decided_by"] == "me" and not out["edited"]
    text = (mem_dir(env) / "rules.md").read_text()
    assert text == (f"---\ntitle: Ziggy hafızası · demo · Kurallar\ncreated: {today}\nmodified: {today}\ntype: memory\n"
                    f"status: active\ntags: [memory, ziggy, kural]\n---\n# Kurallar\n\n{memory.INTROS['kural']}\n\n"
                    "## M-1 · kural · Run make schema\n- Kural: Run `make schema` after model changes.\n- Neden: drift\n"
                    "- Ne zaman geçerli: dosyalar `api/models/**` · konular schema\n"
                    f"- Kaynak: demo local-1 · Q1-q1 · {today} · onay {today}\n")
    with pytest.raises(memory.Decided):
        memory.decide(t, c["id"], "approve")
    # The user's own text stays byte for byte; the next approval only appends and moves `modified`.
    write(mem_dir(env) / "rules.md", USER_EDITED)
    [c2] = memory.add_candidates(t, [candidate("kural", "Second", "Keep it short.", ticket="local-2")], "retro", "local-2")
    out = memory.decide(t, c2["id"], "approve")
    assert out["entry"] == "M-6"  # max over the files and the candidates' entries + 1
    after = (mem_dir(env) / "rules.md").read_text()
    assert after.startswith(USER_EDITED.replace("modified: 2020-01-01", f"modified: {today}"))
    assert after.endswith("\n## M-6 · kural · Second\n- Kural: Keep it short.\n" f"- Kaynak: demo local-2 · Q1-q1 · {today} · onay {today}\n")


def test_edit_reject_and_replace(env):
    t = target()
    write(mem_dir(env) / "decisions.md", "# Kararlar\n\n## M-7 · karar · Use Mongo for reports\n- Karar: Mongo.\n"
          "- Ne zaman geçerli: dosyalar `api/report/**`\n- Kaynak: demo local-0 · 2026-01-01 · onay 2026-01-01\n\n"
          "## M-8 · karar · Weekly starts Monday\n- Karar: Monday.\n- Ne zaman geçerli: konular week\n")
    one, two, three = memory.add_candidates(t, [
        candidate(scope={"paths": ["api/report/views.py"]}),
        candidate(title="Weeks start on Sunday", main="Sunday.", scope={"topics": ["Week"]}),
        candidate(title="Unrelated", main="Something else.", scope={"paths": ["web/**"]})], "answer", "local-1")
    assert (one["similar"], two["similar"], three["similar"]) == (["M-7"], ["M-8"], [])
    with pytest.raises(memory.MemoryRefused, match="title is empty"):
        memory.decide(t, one["id"], "approve", title="  ")
    out = memory.decide(t, one["id"], "approve", text="Postgres;\n  Mongo is gone.", replace=True)
    assert out["entry"] == "M-9" and out["edited"] and out["replaced"] == ["M-7"]
    text = (mem_dir(env) / "decisions.md").read_text()
    assert "- Kaynak: demo local-0 · 2026-01-01 · onay 2026-01-01\n- Durum: yerini aldı M-9\n\n## M-8" in text
    assert "## M-9 · karar · Use Postgres for reports\n- Karar: Postgres; Mongo is gone.\n" in text
    out = memory.decide(t, two["id"], "approve")  # similar, but not replacing: both stay active
    assert out["replaced"] == [] and {e["id"]: e["status"] for e in memory.entries(t)[0]} == \
        {"M-7": "replaced", "M-8": "active", "M-9": "active", "M-10": "active"}
    out = memory.decide(t, three["id"], "reject")
    assert out["status"] == "rejected" and "Unrelated" not in (mem_dir(env) / "decisions.md").read_text()
    # Forgetting rewrites the Durum line in place, nothing else.
    before = (mem_dir(env) / "decisions.md").read_text()
    memory.forget(t, "m-7")
    assert (mem_dir(env) / "decisions.md").read_text() == before.replace("- Durum: yerini aldı M-9", "- Durum: unutuldu")
    with pytest.raises(memory.Decided):
        memory.forget(t, "M-7")
    with pytest.raises(LookupError):
        memory.forget(t, "M-99")


def test_duplicates_and_secrets_never_reach_the_candidates(env):
    t = target()
    write(env["repo"] / ".env", "API_TOKEN=supersecretvalue123\n")
    write(mem_dir(env) / "rules.md", "## M-1 · kural · Vendor\n- Kural: Never touch `vendor/`.\n")
    added = memory.add_candidates(t, [
        candidate("kural", "Vendor again", "never touch `vendor/`"),
        candidate("kural", "Token", "Call the API with supersecretvalue123 and ghp_abcdefghijklmnopqrstuvwxyz0123"),
        candidate("kural", "Token", "the same title twice"),
        {"kind": "tuzak", "title": "Half", "fields": {"Ne oluyor": "x"}, "sources": []}], "retro", "local-1")
    [c] = added
    assert c["fields"]["Kural"] == "Call the API with *** and ***"
    memory.decide(t, c["id"], "approve")
    assert "supersecret" not in (mem_dir(env) / "rules.md").read_text()


# --- candidates from question rounds ------------------------------------------------------------------------------

def test_every_answered_question_becomes_a_decision_candidate(env):
    script(env, {"plan": [{"verdict": "READY", "filesExpected": ["api/report/weekly.py", "README.md"]}],
                 "verify": [{"verdict": "NEEDS_USER", "questions": [
                     {"id": "q1", "text": "Which day does the `weekly` report start on?", "options": ["Monday", "Sunday"],
                      "recommendation": "Monday"},
                     {"id": "q2", "text": "Export CSV as well?", "options": ["A) yes", "B) no"], "recommendation": "B) no"},
                     {"id": "q3", "text": "Unanswered?"}]}, {"verdict": "READY"}],
                 "implement": [{"verdict": "READY", "_commit": "a.txt"}]})
    cli("new", "feat: weekly report")
    assert cli("run", "local-1").returncode == 3
    assert cli("answer", "local-1", "--text", "q1: Sunday, the customers are in the US\nq2: ok").returncode == 0
    one, two = memory.pending(target())
    assert one["kind"] == "karar" and one["title"] == "Which day does the `weekly` report start on?"
    assert one["fields"] == {"Karar": "Sunday, the customers are in the US",
                             "Neden": "local-1 verify aşamasında soruldu (tur 1); önerilen: Monday"}
    assert one["scope"] == {"paths": ["api/report/**", "README.md"], "topics": ["weekly", "day", "report", "start"]}
    assert one["sources"] == [{"ticket": "local-1", "ref": "Q1-q1", "kind": "answer", "where": "02-verify",
                               "summary": "Which day does the `weekly` report start on?"}]
    assert two["fields"]["Karar"] == "B) no" and one["origin"] == "answer"
    [row] = [r for r in attention.items([target()]) if r["ticket"] == attention.MEMORY]
    assert row["title"] == "2 yeni hafıza önerisi"


# --- episodes -----------------------------------------------------------------------------------------------------

def test_done_writes_an_episode_and_the_merge_adds_a_line(env):
    script(env, {"implement": [{"verdict": "READY", "_commit": "api/report.py"}],
                 "review": [{"verdict": "READY", "route": "converged", "converged": True}]})
    cli("new", "feat: weekly totals")
    assert cli("run", "local-1").returncode == 0
    [e] = memory.entries(target())[0]
    today = date.today().isoformat()
    assert e["id"] == "M-1" and e["kind"] == "olay" and e["title"] == "local-1 · weekly totals"
    assert e["fields"]["Ticket"] == "local-1 · feat · hat feat" and e["fields"]["Dokunulan"] == "`api/`"
    assert e["fields"]["Sonuç"].startswith("bitti · maliyet $") and e["fields"]["Sonuç"].endswith("review turu 1")
    assert e["scope"] == {"paths": ["api/**"], "topics": ["weekly", "totals"]}
    assert memory.episode_merged(target(), "local-1", "https://github.com/acme/demo/pull/7")
    assert not memory.episode_merged(target(), "local-1", "https://github.com/acme/demo/pull/7")  # once
    assert (mem_dir(env) / "episodes.md").read_text().endswith(
        f"- Kaynak: demo local-1 · done · {today}\n- Birleşti: {today} · https://github.com/acme/demo/pull/7\n")


def test_pr_sync_records_the_merge(env, monkeypatch):
    from ziggy import cleanup
    t = target()
    cli("new", "feat: five")
    write(mem_dir(env) / "episodes.md", "## M-2 · olay · local-1 · x\n- Ticket: local-1 · feat · hat feat\n")
    st = state.load_state(t, "local-1")
    st.update(status="done", pr="https://github.com/acme/demo/pull/9")
    state.save_state(t, "local-1", st)
    monkeypatch.setattr(cleanup, "_pr", lambda target, url: {"state": "MERGED"})
    monkeypatch.setattr(cleanup.stack, "on_merged", lambda *a, **k: [])
    cleanup.sync(t)
    assert "- Birleşti: " in (mem_dir(env) / "episodes.md").read_text()


# --- recall -------------------------------------------------------------------------------------------------------

MEMORY_FILES = {
    "context.md": "---\ntitle: x\n---\n# Bağlam\n\nThe API is Flask; the worker is Celery.\n",
    "rules.md": "## M-1 · kural · Old rule\n- Kural: Old.\n- Kaynak: demo · 2026-01-01 · onay 2026-01-01\n\n"
                "## M-2 · kural · Used rule\n- Kural: Used a lot.\n\n"
                "## M-3 · kural · Forgotten\n- Kural: x\n- Durum: unutuldu\n",
    "decisions.md": "## M-4 · karar · Reports in Postgres\n- Karar: Postgres.\n- Ne zaman geçerli: dosyalar `api/report/**`\n\n"
                    "## M-5 · karar · Billing in cents\n- Karar: Cents.\n- Ne zaman geçerli: dosyalar `billing/**` · konular invoice\n\n"
                    "## M-6 · karar · Unrelated\n- Karar: No.\n- Ne zaman geçerli: dosyalar `mobile/**`\n\n"
                    "## M-7 · karar · Replaced\n- Karar: Mongo.\n- Ne zaman geçerli: dosyalar `api/report/**`\n- Durum: yerini aldı M-4\n",
    "pitfalls.md": "## M-8 · tuzak · Global\n- Ne oluyor: Flaky clock.\n- Ne yapılır: Freeze time.\n",
    "episodes.md": "## M-9 · olay · local-0 · Weekly invoice\n- Ticket: local-0 · feat · hat feat\n- Dokunulan: `billing/`\n"
                   "- Ne zaman geçerli: dosyalar `billing/**` · konular invoice\n",
}


def seed(env):
    for name, text in MEMORY_FILES.items():
        write(mem_dir(env) / name, text)
    state.write_json(mem_dir(env) / "usage.json", {"M-2": {"count": 5, "last_used": "2026-10-10T00:00:00+00:00", "tickets": ["local-0"]}})


def test_recall_ranks_by_scope_topics_and_use_and_excludes_replaced_and_forgotten(env):
    seed(env)
    ticket = {"id": "local-1", "title": "Fix the invoice total", "body": "The invoice rounds wrong."}
    out = memory.recall(target(), ticket, _Stage("implement"), ["api/report/weekly.py"])
    assert out["exists"] and out["ids"] == ["M-2", "M-1", "M-4", "M-5", "M-8"]  # rules by use; then score order
    text = out["text"]
    assert text.startswith("```markdown\n### Bağlam (context.md)\nThe API is Flask") and text.endswith("\n```")
    assert "M-3" not in text and "M-7" not in text and "M-6" not in text and "M-9" not in text  # episodes: plan stages only
    plan = memory.recall(target(), ticket, _Stage("plan"), [])
    assert "### Benzer eski işler\n- M-9 · local-0 · Weekly invoice · `billing/`" in plan["text"] and "M-4" not in plan["ids"]


def test_recall_keeps_to_the_budget(env):
    seed(env)
    env["config"].write_text(env["config"].read_text().replace('gh_user = "tester"', 'gh_user = "tester"\nmemory_budget = 256'))
    out = memory.recall(target(), {"id": "local-1", "title": "x", "body": ""}, _Stage("implement"), ["api/report/a.py"])
    inner = out["text"].split("\n", 1)[1].rsplit("\n", 1)[0]
    assert len(inner.split("\n(")[0].encode()) <= 256 and out["left_out"] > 0
    assert inner.endswith("kayıt yer kalmadığı için gösterilmedi.)")
    assert memory.recall(target(), {"id": "local-1", "title": "x"}, _Stage("plan"), [])["exists"]
    for name in MEMORY_FILES:
        (mem_dir(env) / name).unlink()
    assert memory.recall(target(), {"id": "local-1", "title": "x"}, _Stage("plan"), []) == \
        {"exists": False, "text": "", "ids": [], "left_out": 0, "warnings": []}


def test_every_stage_prompt_gets_the_memory_and_usage_is_counted(env):
    seed(env)
    script(env, {"plan": [{"verdict": "READY", "memoryUsed": ["m-4", "M-99", "x"]}],
                 "implement": [{"verdict": "READY", "_commit": "api/report/a.py", "memoryUsed": ["M-2"]}],
                 "review": [{"verdict": "READY", "route": "converged", "converged": True, "memoryUsed": ["M-2"],
                             "findings": [{"id": "R1-1", "disposition": "dismiss", "memory": "M-8", "summary": "s"}]}]})
    cli("new", "chore: tidy the report")
    assert cli("run", "local-1").returncode == 0
    for c in calls(env):
        if c.get("stage"):
            assert "## Proje hafızası (project memory, data)" in c["prompt"] and "M-2 · kural · Used rule" in c["prompt"]
            assert "`memoryUsed`" in c["prompt"]
    review = [c for c in calls(env) if c.get("stage") == "review"][0]["prompt"]
    assert "**Project memory** (yours, not a lens's)" in review
    uses = memory.usage(target())
    assert uses["M-2"]["count"] == 7 and uses["M-2"]["tickets"] == ["local-0", "local-1"]
    assert uses["M-4"]["count"] == 1 and uses["M-8"]["count"] == 1 and "M-99" not in uses
    from ziggy import dashboard
    view = dashboard.ticket_detail(target(), "local-1")["memory"]
    assert [(s["stage"], [u["id"] for u in s["used"]]) for s in view["sessions"]] == \
        [("plan", ["M-4"]), ("implement", ["M-2"]), ("review", ["M-2", "M-8"])]
    assert view["sessions"][0]["used"][0]["title"] == "Reports in Postgres"


def test_an_unreadable_memory_file_never_stops_a_stage(env):
    script(env, {"implement": [{"verdict": "READY", "_commit": "a.txt"}]})
    cli("new", "chore: tidy")
    write(mem_dir(env) / "rules.md", "## M-1 · kural · x\n- Kural: y\n")
    (mem_dir(env) / "rules.md").chmod(0)
    try:
        assert cli("run", "local-1").returncode == 0
    finally:
        (mem_dir(env) / "rules.md").chmod(0o644)
    plan = json.loads((env["state"] / "runs" / "local-1" / "01-plan" / "run.json").read_text())
    assert plan["memory"]["shown"] == [] and plan["memory"]["warnings"][0].startswith("rules.md: okunamadı")


def test_stale_entries_are_marked_not_removed(env):
    old = (date.today() - timedelta(days=memory.STALE_DAYS + 1)).isoformat()
    recent = (date.today() - timedelta(days=3)).isoformat()
    write(mem_dir(env) / "decisions.md", f"## M-1 · karar · Old\n- Karar: a\n- Kaynak: demo · {old} · onay {old}\n\n"
          f"## M-2 · karar · Old but used\n- Karar: b\n- Kaynak: demo · {old} · onay {old}\n\n"
          f"## M-3 · karar · New\n- Karar: c\n- Kaynak: demo · {recent} · onay {recent}\n\n"
          "## M-4 · karar · Marked by hand\n- Karar: d\n- Durum: soluk\n")
    write(mem_dir(env) / "rules.md", f"## M-5 · kural · Old rule\n- Kural: e\n- Kaynak: demo · {old} · onay {old}\n")
    state.write_json(mem_dir(env) / "usage.json", {"M-2": {"count": 1, "last_used": f"{recent}T00:00:00+00:00", "tickets": []}})
    view = memory.view(target())
    assert {e["id"]: e["stale"] for e in view["entries"]["karar"]} == {"M-1": True, "M-2": False, "M-3": False, "M-4": True}
    assert view["entries"]["kural"][0]["stale"] is False  # rules never go stale
    assert len(memory.entries(target())[0]) == 5


# --- migration ------------------------------------------------------------------------------------------------------

def test_migration_of_context_learned_rules_and_candidates_is_idempotent(env):
    old = write(env["config"].parent / "targets" / "demo" / "context.md",
                "# Demo\n\n- Never touch `vendor/`.\n\n## Learned rules\n\n"
                "- Run `make schema` after model changes. (kaynak: local-0 U1-1, 2026-01-02)\n"
                "- Keep dates in UTC. (kaynak: local-2 R1-3, local-2 Q1-q1, 2026-02-03)\n\n## Other\n\nkept\n")
    (env["state"] / "rules").mkdir(parents=True)
    state.write_json(env["state"] / "rules" / "candidates.json", {"next": 4, "candidates": [
        {"id": "c1", "rule": "Run `make schema` after model changes.", "status": "approved", "sources": []},
        {"id": "c2", "rule": "Prefer `fmt_money` for amounts.", "why": "twice", "status": "pending", "ticket": "local-3",
         "sources": [{"ticket": "local-3", "ref": "U1-1", "kind": "user_comment", "summary": "use fmt_money"}],
         "created_at": "2026-03-01T00:00:00+00:00"},
        {"id": "c3", "rule": "Write good tests.", "status": "rejected", "decided_at": "2026-03-02T00:00:00+00:00"}]})
    t = target()
    view = memory.view(t)  # any use migrates
    context = (mem_dir(env) / "context.md").read_text()
    assert context.endswith("# Bağlam\n\n" + memory.INTROS["context"] + "\n\n# Demo\n\n- Never touch `vendor/`.\n\n## Other\n\nkept\n")
    assert memory.context_text(t).startswith("# Demo\n\n- Never touch `vendor/`.")
    m1, m2 = view["entries"]["kural"][::-1]
    assert (m1["id"], m1["fields"]["Kural"]) == ("M-1", "Run `make schema` after model changes.")
    assert m1["fields"]["Kaynak"] == "demo local-0 U1-1 · 2026-01-02 · onay 2026-01-02 · context.md'den taşındı"
    assert m2["fields"]["Kaynak"].startswith("demo local-2 R1-3, local-2 Q1-q1 · 2026-02-03")
    [pending] = view["pending"]
    assert pending["fields"] == {"Kural": "Prefer `fmt_money` for amounts.", "Neden": "twice"} and pending["origin"] == "migration"
    assert pending["ticket"] == "local-3" and pending["sources"][0]["ref"] == "U1-1"
    assert [c["status"] for c in memory.load_candidates(t)["candidates"]] == ["pending", "rejected"]
    before = {p.name: p.read_text() for p in mem_dir(env).iterdir() if p.is_file()}
    assert memory.migrate(t) is None
    memory.view(t)
    assert {p.name: p.read_text() for p in mem_dir(env).iterdir() if p.is_file()} == before
    assert old.exists()  # left where it was, unread from now on


def test_the_old_context_file_no_longer_reaches_the_prompts(env):
    write(env["config"].parent / "targets" / "demo" / "context.md", "OLD-CONTEXT-MARKER\n")
    mem_dir(env).mkdir(parents=True)
    state.write_json(mem_dir(env) / "migrated.json", {"at": "x"})  # migrated before; the user then wrote to it
    script(env, {"implement": [{"verdict": "READY", "_commit": "a.txt"}]})
    cli("new", "chore: tidy")
    assert cli("run", "local-1").returncode == 0
    assert not any("OLD-CONTEXT-MARKER" in c["prompt"] for c in calls(env) if c.get("stage"))


# --- CLI ------------------------------------------------------------------------------------------------------------

def test_cli_lists_decides_and_forgets(env):
    t = target()
    first, second = memory.add_candidates(t, [candidate(), candidate("tuzak", "Clock", "Freeze it.", **{"Ne oluyor": "Flaky."})],
                                          "answer", "local-1")
    out = cli("memory", "candidates")
    assert out.returncode == 0 and f"{first['id']}\tkarar\tUse Postgres for reports" in out.stdout and "Ne oluyor: Flaky." in out.stdout
    out = cli("memory", "approve", first["id"], "--text", "Postgres only.", "--title", "Postgres", "--target", "demo")
    assert out.returncode == 0 and "approved → M-1 in" in out.stdout
    assert cli("memory", "reject", second["id"]).returncode == 0
    assert cli("memory", "approve", second["id"]).returncode == 2
    assert cli("memory", "approve").returncode == 2
    listing = cli("memory")
    assert listing.returncode == 0 and "M-1\tkarar\tPostgres" in listing.stdout and "(state)" in listing.stdout
    assert cli("memory", "forget", "M-1").returncode == 0
    assert "unutuldu" in cli("memory", "list").stdout
    assert cli("rules").returncode == 2  # gone
