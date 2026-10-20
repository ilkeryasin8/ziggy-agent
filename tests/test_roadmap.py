"""The roadmap: roadmap.md in the memory folder (tolerant parse, automatic progress through a ticket's life, scope
changes as `yol` candidates), BMad's epics + sprint-status read-only, the next items, the composer origin, and the
draft session (fake claude as stage `roadmap`)."""
import json
import time

import pytest
from conftest import calls, script

from ziggy import attention, cleanup, config, dashboard, memory, pipeline, publish, results, roadmap, state, ticket

ROADMAP = """---
title: Ziggy hafızası · demo · Yol haritası
created: 2026-10-01
modified: 2026-10-01
type: memory
tags: [memory, ziggy, yol-haritası]
---
# Yol haritası

Benim notum: bu satır kalmalı.

## P1 · Temel
- [ ] R-1 · Haftalık rapor
  - Rapor ekranı ve toplamlar.
- [x] R-2: Proje iskeleti

## P2 - Kayıt
Evrenin açıklaması (serbest metin).
- [ ] R-3 · Öğün kaydı
  - Elle kayıt, sonra tariften.
* [ ] R4 - Gün toplamları
- [ ] R-5 · Düzenle ve sil

## Notlar
Serbest bölüm; okunmaz ama yerinde kalır.
"""


@pytest.fixture
def target(env):
    return config.load_target("demo")


def path(target):
    return roadmap.memory_path(target)


def write(target, text=ROADMAP):
    path(target).parent.mkdir(parents=True, exist_ok=True)
    path(target).write_text(text, encoding="utf-8")


def text(target):
    return path(target).read_text(encoding="utf-8")


def item(target, item_id):
    return roadmap.find(roadmap.load(target), item_id)[1]


def phase(target, phase_id):
    return next(p for p in roadmap.load(target)["phases"] if p["id"] == phase_id)


# --- parsing --------------------------------------------------------------------------------------------------------

def test_memory_source_is_parsed_tolerantly(target):
    assert roadmap.load(target) == {"source": None, "path": None, "warnings": [], "phases": [], "shadowed": None}
    write(target)
    data = roadmap.load(target)
    assert data["source"] == "memory" and data["path"] == str(target.state_dir / "memory" / "roadmap.md") and data["warnings"] == []
    assert [(p["id"], p["title"], p["status"]) for p in data["phases"]] == [("P1", "Temel", "todo"), ("P2", "Kayıt", "todo")]
    assert [(i["id"], i["title"], i["status"], i["detail"]) for i in data["phases"][1]["items"]] == [
        ("R-3", "Öğün kaydı", "todo", "Elle kayıt, sonra tariften."), ("R-4", "Gün toplamları", "todo", ""),
        ("R-5", "Düzenle ve sil", "todo", "")]
    assert item(target, "R-2")["status"] == "done"  # checked by hand


BROKEN = """---
title: x
---
## P1 · Temel
- Durum: belki
- [ ] Kimliksiz madde
- [ ] R-1 · Bir
- [ ] R-1 · Yine bir
## P2x yanlış
- [ ] R-2 · evresiz
## P3
- [ ] R-3 · üç
  - Ticket: local-9 · Durum: uçuyor
"""


def test_broken_markdown_gives_warnings_not_errors(target):
    write(target, BROKEN)
    data = roadmap.load(target)
    warnings = "\n".join(data["warnings"])
    for expected in ("roadmap.md:5: P1 durumu okunamadı: belki", "roadmap.md:6: maddenin kimliği yok (R-<n>): Kimliksiz madde",
                     "roadmap.md:8: R-1 roadmap.md:7 içinde de var", "roadmap.md:9: evre başlığı okunamadı: P2x yanlış",
                     "roadmap.md:10: madde bir evrenin (## P1 · başlık) altında değil: - [ ] R-2", "roadmap.md:11: P3 başlıksız",
                     "roadmap.md:13: R-3 durumu okunamadı: uçuyor"):
        assert expected in warnings
    assert [(p["id"], [i["id"] for i in p["items"]]) for p in data["phases"]] == [("P1", ["R-1"]), ("P3", ["R-3"])]
    assert item(target, "R-3")["status"] == "ticket" and item(target, "R-1")["title"] == "Bir"
    # The Hafıza page and the inbox's memory row show them like the memory files' warnings.
    assert set(data["warnings"]) <= set(memory.view(target)["warnings"])
    _, need = attention.memory_current(target)
    assert need["kind"] == "memory" and "sorun" in need["title"]
    write(target, "---\ntitle: x\n# no closing fence\n## P1 · a\n- [ ] R-1 · b\n")
    assert "frontmatter kapanmamış" in roadmap.load(target)["warnings"][0]


# --- progress through a ticket's life --------------------------------------------------------------------------------

def test_a_ticket_moves_its_item_from_created_to_merged(env, target, monkeypatch):
    write(target)
    seen = []
    real = roadmap._progress
    monkeypatch.setattr(roadmap, "_progress", lambda tg, i, tk, status, pr: seen.append(status) or real(tg, i, tk, status, pr))
    out = dashboard.new_ticket(target, {"title": "Haftalık rapor", "type": "chore", "body": "R-1",
                                        "origin": {"kind": "roadmap", "item": "R-1"}})
    tid = out["ticket"]["id"]
    assert state.load_ticket(target, tid)["origin"] == {"kind": "roadmap", "item": "R-1", "phase": "P1"}
    assert item(target, "R-1")["status"] == "ticket" and item(target, "R-1")["ticket"] == tid
    lines = text(target).split("\n")
    at = lines.index("- [ ] R-1 · Haftalık rapor")
    assert lines[at + 1] == f"  - Ticket: {tid} · Durum: ticket" and lines[at + 2] == "  - Rapor ekranı ve toplamlar."
    assert lines[lines.index("## P1 · Temel") + 1] == "- Durum: aktif"  # no phase was active: the first unfinished one is
    assert roadmap.links(target) == {"R-1": tid}

    script(env, {"implement": [{"verdict": "READY", "_commit": "report.txt"}],
                 "review": [{"verdict": "READY", "route": "converged", "converged": True, "findings": []}]})
    pipeline.run(target, tid)
    assert state.load_state(target, tid)["status"] == "done"
    assert "in-progress" in seen and seen[-1] == "review" and item(target, "R-1")["status"] == "review"

    url = publish.publish(target, tid)
    assert item(target, "R-1")["status"] == "pr" and item(target, "R-1")["pr"] == url
    assert f"  - Ticket: {tid} · PR: {url} · Durum: pr" in text(target)

    monkeypatch.setenv("FAKE_GH_PR_STATE", "MERGED")
    cleanup.sync(target)
    assert item(target, "R-1")["status"] == "merged"
    body = text(target)
    assert "- [x] R-1 · Haftalık rapor" in body and f"Durum: merged" in body
    # P1 is all merged or done: bitti; P2 becomes the active phase.
    assert phase(target, "P1")["status"] == "done" and phase(target, "P2")["status"] == "active"
    lines = body.split("\n")
    assert lines[lines.index("## P1 · Temel") + 1] == "- Durum: bitti" and lines[lines.index("## P2 - Kayıt") + 1] == "- Durum: aktif"
    # Nothing else moved: the user's lines are where they were.
    for keep in ("Benim notum: bu satır kalmalı.", "Evrenin açıklaması (serbest metin).", "* [ ] R4 - Gün toplamları",
                 "## Notlar", "Serbest bölüm; okunmaz ama yerinde kalır.", "- [x] R-2: Proje iskeleti"):
        assert keep in lines
    assert [line for line in lines if line.startswith("modified:")] == [f"modified: {memory._today()}"]
    assert roadmap.ticket_changed(target, tid) is False  # idempotent: nothing changes again


def test_progress_never_moves_back_and_a_checked_item_stays_done(target):
    write(target)
    t = ticket.create(target, "Öğün kaydı", "feat", "", False, "low", origin={"kind": "roadmap", "item": "R-3", "phase": "P2"})
    st = state.load_state(target, t["id"])
    st.update(status="running")
    state.save_state(target, t["id"], st)
    roadmap.ticket_changed(target, t["id"])
    assert item(target, "R-3")["status"] == "in-progress"
    st.update(status="new")
    state.save_state(target, t["id"], st)
    assert roadmap.ticket_changed(target, t["id"]) is False and item(target, "R-3")["status"] == "in-progress"
    path(target).write_text(text(target).replace("- [ ] R-3 · Öğün kaydı", "- [x] R-3 · Öğün kaydı"))
    st.update(status="running")
    state.save_state(target, t["id"], st)
    roadmap.ticket_changed(target, t["id"])
    assert item(target, "R-3")["status"] == "done"


def test_link_by_hand_and_errors(target):
    write(target)
    t = ticket.create(target, "Gün toplamları", "feat", "", False, "low")
    with pytest.raises(LookupError):
        roadmap.link(target, "R-99", t["id"])
    with pytest.raises(LookupError):
        roadmap.link(target, "R-4", "local-404")
    roadmap.link(target, "r4", t["id"])
    assert roadmap.links(target) == {"R-4": t["id"]} and item(target, "R-4")["ticket"] == t["id"]
    assert roadmap.ticket_ref(target, t["id"]) == {"item": "R-4", "phase": "P2", "title": "Gün toplamları"}
    assert "  - Ticket: local-1 · Durum: ticket" in text(target)


def test_a_missing_or_broken_roadmap_never_breaks_a_ticket(target, monkeypatch):
    t = ticket.create(target, "x", "feat", "", False, "low", origin={"kind": "roadmap", "item": "R-1", "phase": "P1"})
    assert roadmap.ticket_changed(target, t["id"]) is False  # no roadmap.md: nothing to write
    write(target)
    monkeypatch.setattr(roadmap, "_progress", lambda *a: 1 / 0)
    assert roadmap.ticket_changed(target, t["id"]) is False


# --- next items -------------------------------------------------------------------------------------------------------

def test_next_items_follow_the_active_phase_in_order(target):
    assert roadmap.next_items(target) == []
    write(target)
    assert [i["id"] for i in roadmap.next_items(target)] == ["R-1"]  # none active: the first unfinished phase
    path(target).write_text(text(target).replace("## P2 - Kayıt", "## P2 - Kayıt\n- Durum: aktif"))
    t = ticket.create(target, "Öğün kaydı", "feat", "", False, "low", origin={"kind": "roadmap", "item": "R-3", "phase": "P2"})
    nxt = roadmap.next_items(target)
    assert [(i["id"], i["phase"]) for i in nxt] == [("R-4", "P2"), ("R-5", "P2")]
    assert roadmap.next_items(target, n=1)[0]["title"] == "Gün toplamları"
    assert item(target, "R-3")["ticket"] == t["id"]


# --- the composer origin -----------------------------------------------------------------------------------------------

def test_origin_roadmap_round_trip(env, target):
    write(target)
    with pytest.raises(LookupError):
        dashboard.new_ticket(target, {"title": "x", "origin": {"kind": "roadmap", "item": "R-77"}})
    with pytest.raises(ValueError, match="R-2 is done"):
        dashboard.new_ticket(target, {"title": "x", "origin": {"kind": "roadmap", "item": "R-2"}})
    out = dashboard.new_ticket(target, {"title": "Düzenle ve sil", "body": "R-5", "origin": {"kind": "roadmap", "item": "R-5"}})
    tid = out["ticket"]["id"]
    assert out["ticket"]["source"] == "local" and tid == "local-1"
    md = (state.run_dir(target, tid) / "ticket.md").read_text()
    assert "- Source: roadmap item R-5 (phase P2) of this repository's long-term plan." in md
    with pytest.raises(ValueError, match="already ticket local-1"):
        dashboard.new_ticket(target, {"title": "again", "origin": {"kind": "roadmap", "item": "R-5"}})
    row = dashboard.ticket_row(target, tid)
    assert row["origin"]["kind"] == "roadmap" and row["roadmap"] == {"item": "R-5", "phase": "P2", "title": "Düzenle ve sil"}
    view = roadmap.view(target)
    assert view["where"] == "state" and view["current"] == "P1" and view["next"] == ["R-1"]
    assert [(p["id"], p["done"], p["total"]) for p in view["phases"]] == [("P1", 1, 2), ("P2", 0, 3)]
    # A plain ticket has none.
    plain = ticket.create(target, "y", "feat", "", False, "low")
    assert dashboard.ticket_row(target, plain["id"])["roadmap"] is None


# --- scope changes: yol candidates ----------------------------------------------------------------------------------------

def test_scope_changes_are_candidates_and_approving_edits_the_file(target):
    write(target)
    added = roadmap.propose(target, [
        {"op": "add", "phase": "p2", "title": "Tariften kayıt", "detail": "Tarif seçilince porsiyon sorulur.", "why": "Sık istenen."},
        {"op": "remove", "item": "R-5"},
        {"op": "move", "item": "R-4", "phase": "P1"},
        {"op": "phase", "title": "Yayın"},
        {"op": "add", "title": "evresiz"},  # no phase: skipped
        {"op": "bogus"},
    ], "lead")
    assert [c["fields"]["İşlem"] for c in added] == ["ekle", "çıkar", "taşı", "evre ekle"]
    assert {c["kind"] for c in memory.pending(target)} == {"yol"} and added[0]["fields"]["Evre"] == "P2"
    assert [c["title"] for c in memory.view(target)["pending"]] == [c["title"] for c in added]
    before = text(target)

    memory.decide(target, added[1]["id"], "reject")
    assert text(target) == before  # a rejected change writes nothing

    c = memory.decide(target, added[0]["id"], "approve", text="Tariften öğün kaydı")
    assert c["status"] == "approved" and c["entry"] == "R-6" and c["edited"]
    lines = text(target).split("\n")
    at = lines.index("- [ ] R-6 · Tariften öğün kaydı")
    assert lines[at - 1] == "- [ ] R-5 · Düzenle ve sil" and lines[at + 1] == "  - Tarif seçilince porsiyon sorulur."

    assert memory.decide(target, added[2]["id"], "approve")["entry"] == "R-4"
    assert [i["id"] for i in phase(target, "P1")["items"]] == ["R-1", "R-2", "R-4"]
    assert [i["id"] for i in phase(target, "P2")["items"]] == ["R-3", "R-5", "R-6"]

    assert memory.decide(target, added[3]["id"], "approve")["entry"] == "P3"
    assert phase(target, "P3")["title"] == "Yayın" and "## Notlar" in text(target)

    remove = roadmap.propose(target, [{"op": "remove", "item": "R-5", "why": "Kapsam dışı."}], "user")
    memory.decide(target, remove[0]["id"], "approve")
    assert roadmap.find(roadmap.load(target), "R-5") is None and "Düzenle ve sil" not in text(target)
    assert roadmap.load(target)["warnings"] == []

    gone = roadmap.propose(target, [{"op": "move", "item": "R-77", "phase": "P1"}], "user")
    with pytest.raises(memory.MemoryRefused, match="R-77"):
        memory.decide(target, gone[0]["id"], "approve")
    assert memory.pending(target)[0]["id"] == gone[0]["id"]  # still pending after a refused approval


# --- BMad ------------------------------------------------------------------------------------------------------------------

EPICS = """---
stepsCompleted: [step-01]
---

# Demo - Epic Breakdown

## Epic List

### Epic 1: Tarifler — my recipes
After this epic the user can save recipes.

### Epic 2: Günlük takip
Log meals.

## Epic 1: Tarifler — my recipes

The user can save their own recipes.

### Story 1.1: Set up the project

As the owner,
I want a buildable app,
So that later stories build on it.

**Acceptance Criteria:**

**Given** a Mac
**When** set up
**Then** it builds

### Story 1.2: Create a Tarif

As a home cook,
I want to save a Tarif.

### Story 1.3: Delete a Tarif

As a home cook, I want to delete one.

## Epic 2: Günlük takip — log what I eat

### Story 2.1: Log a meal by hand

As the owner, I want to log a meal.

### Story 2.2: See the day's totals

As the owner, I want totals.

### Story 2.3: Estimate from text
"""

SPRINT = """# STATUS DEFINITIONS:
#   - backlog: Story only exists in epic file
generated: 10-14-2026 11:33
project: Demo
development_status:
  epic-1: in-progress
  1-1-set-up-the-project: done
  1-2-create-a-tarif: review
  1-3-delete-a-tarif: ready-for-dev
  epic-1-retrospective: optional

  epic-2: backlog
  2-1-log-a-meal-by-hand: in-progress
  2-2-see-the-day-s-totals: drafted   # an older BMad's status
  2-3-estimate-from-text: waiting-for-godot
  9-9-not-in-epics: backlog
action_items: []
"""


def bmad_files(env):
    epics, sprint = env["repo"] / roadmap.EPICS, env["repo"] / roadmap.SPRINT
    epics.parent.mkdir(parents=True)
    sprint.parent.mkdir(parents=True)
    epics.write_text(EPICS)
    sprint.write_text(SPRINT)
    return epics, sprint


def test_bmad_source_is_read_only_and_wins(env, target):
    write(target)
    epics, sprint = bmad_files(env)
    data = roadmap.load(target)
    assert data["source"] == "bmad" and data["path"] == str(epics) and data["shadowed"] == str(path(target))
    assert [(p["id"], p["title"], p["status"]) for p in data["phases"]] == [("E1", "Tarifler — my recipes", "active"),
                                                                           ("E2", "Günlük takip — log what I eat", "active")]
    assert [(i["id"], i["status"]) for p in data["phases"] for i in p["items"]] == [
        ("1-1", "done"), ("1-2", "review"), ("1-3", "todo"), ("2-1", "in-progress"), ("2-2", "todo"), ("2-3", "todo")]
    s11 = data["phases"][0]["items"][0]
    assert s11["title"] == "Set up the project" and s11["detail"] == "As the owner, I want a buildable app, So that later stories build on it."
    warnings = "\n".join(data["warnings"])
    assert "2-3-estimate-from-text: bilinmeyen durum 'waiting-for-godot'" in warnings and "9-9-not-in-epics epics.md'de yok" in warnings
    assert "optional" not in warnings and "drafted" not in warnings
    assert [i["id"] for i in roadmap.next_items(target)] == ["1-3"]
    assert roadmap.view(target)["where"] == "bmad"

    # A ticket from a story: linked in the state, its progress shown over the sprint status, the files untouched.
    before = (epics.read_bytes(), sprint.read_bytes(), text(target))
    out = dashboard.new_ticket(target, {"title": "Delete a Tarif", "origin": {"kind": "roadmap", "item": "1-3"}})
    tid = out["ticket"]["id"]
    assert roadmap.links(target) == {"1-3": tid}
    st = state.load_state(target, tid)
    st.update(status="done", pr="https://github.example.test/acme/demo/pull/9")
    state.save_state(target, tid, st)
    roadmap.ticket_changed(target, tid)
    s13 = item(target, "1-3")
    assert (s13["status"], s13["ticket"], s13["pr"]) == ("pr", tid, "https://github.example.test/acme/demo/pull/9")
    assert (epics.read_bytes(), sprint.read_bytes(), text(target)) == before
    assert roadmap.ticket_ref(target, tid) == {"item": "1-3", "phase": "E1", "title": "Delete a Tarif"}

    # Scope changes go to BMad, not to Ziggy.
    [c] = roadmap.propose(target, [{"op": "add", "phase": "P1", "title": "x"}], "user")
    with pytest.raises(memory.MemoryRefused, match="BMad"):
        memory.decide(target, c["id"], "approve")
    with pytest.raises(roadmap.RoadmapError):
        roadmap.save(target, "## P1 · a\n- [ ] R-1 · b\n")


def test_sprint_status_subset_reader():
    out, warnings = roadmap.sprint_status("a: 1\ndevelopment_status:\n  x-1: done  # c\n  'y': \"review\"\n  bad line\nother:\n  z: q\n")
    assert out == {"x-1": "done", "y": "review"} and warnings == ["sprint-status.yaml:5: okunamadı: bad line"]


# --- the draft -------------------------------------------------------------------------------------------------------------

DRAFT = {"verdict": "READY", "summary": "İki evre", "_structured": True, "notes": ["Ödeme kapsam dışı sayıldı."], "phases": [
    {"title": "Temel", "goal": "Uygulama açılır.", "items": [{"title": "İskelet", "detail": "README'deki yapı."}, {"title": "Giriş"}]},
    {"title": "Rapor", "items": [{"title": "Haftalık rapor", "detail": "Toplamlar."}, {"title": ""}]},
    {"title": "Boş evre", "items": []},
]}


def roadmap_calls(env):
    return [c for c in calls(env) if c.get("stage") == "roadmap"]


def test_draft_session_proposes_and_the_user_saves(env, target):
    memory_dir = roadmap.memory_path(target).parent
    memory_dir.mkdir(parents=True)
    (memory_dir / "context.md").write_text("---\ntitle: c\n---\n# Bağlam\n\nAile içi kullanım; tek kullanıcı.\n")
    (memory_dir / "decisions.md").write_text("## M-1 · karar · SQLite\n- Karar: Veri yerelde SQLite'ta.\n")
    (env["repo"] / "docs").mkdir()
    (env["repo"] / "docs" / "PROJECT.md").write_text("# Demo\n")
    script(env, {"roadmap": [DRAFT]})
    record = roadmap.run_draft(target)
    assert record["state"] == "ok" and record["verdict"] == "READY"
    assert record["draft"] == ("## P1 · Temel\n- Durum: aktif\nUygulama açılır.\n- [ ] R-1 · İskelet\n  - README'deki yapı.\n"
                               "- [ ] R-2 · Giriş\n\n## P2 · Rapor\n- [ ] R-3 · Haftalık rapor\n  - Toplamlar.\n")
    [call] = roadmap_calls(env)
    argv = call["argv"]
    assert argv[argv.index("--model") + 1] == "sonnet" and argv[argv.index("--max-budget-usd") + 1] == "1"
    denied = argv[argv.index("--disallowedTools") + 1:argv.index("--session-id")]
    assert {"Edit", "Write", "MultiEdit", "Bash", "Task", "Read(**/.env)"} <= set(denied)
    assert call["cwd"] == str(env["repo"]) and '"phases"' in argv[argv.index("--json-schema") + 1]
    assert "`docs/PROJECT.md`" in call["prompt"] and "`README.md`" in call["prompt"]
    assert "Aile içi kullanım" in call["prompt"] and "M-1 · SQLite: Veri yerelde SQLite'ta." in call["prompt"]
    assert not path(target).exists()  # nothing is written before the user saves

    view = roadmap.view(target)
    assert view["source"] is None and view["draft"]["draft"] == record["draft"] and view["draft"]["notes"] == ["Ödeme kapsam dışı sayıldı."]
    assert view["draft"]["summary"] == "İki evre" and view["draft"]["running"] is False
    edited = record["draft"].replace("Giriş", "Giriş ekranı")
    with pytest.raises(ValueError, match="no phase"):
        dashboard.roadmap_post(target, "save", {"text": "sadece metin"})
    saved = dashboard.roadmap_post(target, "save", {"text": edited})
    assert saved["source"] == "memory" and item(target, "R-2")["title"] == "Giriş ekranı"
    assert text(target).startswith("---\ntitle: Ziggy hafızası · demo · Yol haritası\n") and "type: memory" in text(target)
    with pytest.raises(dashboard.Refused, match="already has a roadmap"):
        dashboard.roadmap_post(target, "draft", {})
    with pytest.raises(dashboard.Refused, match="already exists"):  # once saved, roadmap.md is edited in Obsidian
        dashboard.roadmap_post(target, "save", {"text": edited})
    # The cost counts as a `roadmap` session in the stats.
    assert any(s["name"] == "roadmap" for s in dashboard.stats()["by_stage"])


def test_draft_from_the_panel_runs_as_a_job(env, target):
    script(env, {"roadmap": [{k: v for k, v in DRAFT.items() if k != "_structured"}]})
    out = dashboard.roadmap_post(target, "draft", {})
    assert out["job"]["command"] == "roadmap draft"
    with pytest.raises(dashboard.Refused, match="already being written"):
        dashboard.roadmap_post(target, "draft", {})
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and roadmap.draft_view(target)["running"]:
        time.sleep(0.2)
    d = roadmap.draft_view(target)
    assert d["state"] == "ok" and d["draft"].startswith("## P1 · Temel")


def test_a_failed_draft_has_no_markdown(env, target):
    script(env, {"roadmap": [{"verdict": "FAILED", "summary": "boş repo", "phases": []}]})
    record = roadmap.run_draft(target)
    assert record["state"] == "ok" and record["draft"] is None
    assert roadmap.draft_view(target)["draft"] is None


def test_results_schema_for_roadmap_is_engine_only():
    assert "roadmap" not in results.NAMES
    assert "memoryUsed" not in results.json_schema("roadmap")["properties"]
    out, errors = results.normalize("roadmap", {"verdict": "READY", "phases": [
        {"title": " A  b ", "items": [{"title": "x", "detail": 3}, "junk"]}, {"title": "", "items": [{"title": "y"}]}]})
    assert errors == [] and out["phases"] == [{"title": "A b", "goal": "", "items": [{"title": "x", "detail": ""}]}]
    assert json.dumps(results.json_schema("roadmap"))


def test_cli_show_and_link(env, target):
    from conftest import cli
    proc = cli("roadmap")
    assert proc.returncode == 0 and "no roadmap yet" in proc.stdout
    write(target)
    t = ticket.create(target, "Rapor", "feat", "", False, "low")
    assert cli("roadmap", "link", "R-1", t["id"]).returncode == 0
    proc = cli("roadmap", "show")
    assert proc.returncode == 0 and "P1\tactive\t1/2\tTemel" in proc.stdout and f"R-1\tticket\tHaftalık rapor\t({t['id']})" in proc.stdout
