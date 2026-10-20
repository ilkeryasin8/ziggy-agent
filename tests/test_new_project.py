"""New projects started from nothing: README, AGENTS.md, .gitignore and a docs/PROJECT.md stub in one
first commit, a GitHub repository (tests/fake_gh.py), and a target in the config."""
import pytest
from conftest import gh_calls, git

from ziggy import config, new_project


@pytest.fixture
def remotes(env, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_GH_REMOTES", str(tmp_path / "remotes"))
    monkeypatch.setenv("ZIGGY_GITHUB_URL", str(tmp_path / "remotes"))
    # A brand-new repository takes the machine's git identity; CI has none configured.
    for key, value in (("NAME", "Test"), ("EMAIL", "test@example.test")):
        monkeypatch.setenv(f"GIT_AUTHOR_{key}", value)
        monkeypatch.setenv(f"GIT_COMMITTER_{key}", value)
    return env


def test_new_project_from_nothing(remotes, tmp_path):
    env = remotes
    dev = tmp_path / "dev"
    dev.mkdir()
    env["config"].write_text(env["config"].read_text() + f'\n[onboard]\ndefault_gh_user = "tester"\n[onboard.gh_users]\n"{dev}" = "tester"\n')
    assert str(dev) in new_project.project_parents()

    name = new_project.create_project("wardrobe", str(dev), "Plan outfits from what is in the closet.")
    assert name == "wardrobe"
    repo = dev / "wardrobe"
    assert "Plan outfits" in (repo / "README.md").read_text() and "Plan outfits" in (repo / "AGENTS.md").read_text()
    doc = (repo / "docs" / "PROJECT.md").read_text()
    assert doc.startswith("# wardrobe\n") and "## Product\n\nPlan outfits" in doc and "## Open questions" in doc
    assert sorted(git("ls-files", cwd=repo).split()) == [".gitignore", "AGENTS.md", "README.md", "docs/PROJECT.md"]
    assert git("log", "--format=%s", cwd=repo).splitlines() == ["chore: start wardrobe"]
    assert git("status", "--porcelain", cwd=repo) == ""
    bare = tmp_path / "remotes" / "tester" / "wardrobe.git"
    assert git("--git-dir", str(bare), "rev-parse", "main", cwd=tmp_path).strip() == git("rev-parse", "HEAD", cwd=repo).strip()
    create = next(c["argv"] for c in gh_calls(env) if c["argv"][:2] == ["repo", "create"])
    assert create[2] == "tester/wardrobe" and "--private" in create

    t = config.load_target("wardrobe")
    assert t.github_repo == "tester/wardrobe" and t.gh_user == "tester"

    with pytest.raises(new_project.SetupError, match="already exists"):
        new_project.create_project("wardrobe", str(dev))
    with pytest.raises(new_project.SetupError, match="not one of the project folders"):
        new_project.create_project("other", str(tmp_path))
    with pytest.raises(new_project.SetupError, match="project name"):
        new_project.create_project("bad name", str(dev))


def test_new_project_offers_gh_owners_and_claude_logins(remotes, tmp_path):
    env = remotes
    dev = tmp_path / "dev"
    dev.mkdir()
    login = tmp_path / ".claude-work"
    env["config"].write_text(env["config"].read_text() + f'\n[onboard]\ndefault_gh_user = "tester"\n[onboard.gh_users]\n"{dev}" = "tester"\n'
                             f'[onboard.claude_config_dirs]\n"{tmp_path / "elsewhere"}" = "{login}"\n')
    new_project._CHOICES.clear()
    d = new_project.project_defaults(str(dev), "x")
    assert {(o["owner"], o["gh_user"], o["kind"]) for o in d["owners"]} == {
        ("tester", "tester", "user"), ("work-me", "work-me", "user"), ("acme-org", "work-me", "org")}
    assert [login_["dir"] for login_ in d["claude_logins"]] == [None, str(login)]

    name = new_project.create_project("shop", str(dev), owner="acme-org", gh_user="work-me", claude_config_dir=str(login))
    t = config.load_target(name)
    assert t.github_repo == "acme-org/shop" and t.gh_user == "work-me" and t.env["CLAUDE_CONFIG_DIR"] == str(login)
    create = next(c for c in gh_calls(env) if c["argv"][:2] == ["repo", "create"])
    assert create["argv"][2] == "acme-org/shop" and create["gh_token"] == "fake-token-for-work-me"
