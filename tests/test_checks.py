"""Engine-run checks: sequencing, log framing, outcome details and the sandbox-exec confinement."""
import os
import re
import signal
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest

from ziggy import checks, config
from ziggy.dispatch import PID_FILE, STOP_FILE

HAS_SANDBOX = checks.available()


@pytest.fixture
def any_ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = config.Target(name="demo", path=tmp_path / "repo")
    wt = tmp_path / "wt"
    wt.mkdir()
    stage_dir = target.state_dir / "runs" / "t1" / "03-implement"
    log = stage_dir / "checks.log"
    env = {**os.environ, "ZIGGY_RUN": "1"}

    def run(*items):
        specs = [{"name": n, "run": r, "timeout_s": t} for n, r, t in items]
        return checks.run(target, "t1", wt, specs, log, env)

    return {"target": target, "wt": wt, "log": log, "run": run, "env": env}


@pytest.fixture
def ctx(any_ctx):
    """Checks that really execute need sandbox-exec (macOS); without it they must refuse (see below)."""
    if not HAS_SANDBOX:
        pytest.skip("needs an OS sandbox for checks (sandbox-exec or bwrap)")
    return any_ctx


def test_pass_records_outcome_and_last_three_lines(ctx):
    [out] = ctx["run"](("tests", "for i in 1 2 3 4 5; do echo line$i; done", 60))
    assert out["name"] == "tests" and out["status"] == "pass" and out["source"] == "engine"
    assert out["exit_code"] == 0 and isinstance(out["duration_ms"], int)
    assert out["detail"] == "line3\nline4\nline5"
    assert not (ctx["log"].parent / PID_FILE).exists()


def test_fail_stops_at_first_failure_with_how_and_ansi_stripped_tail(ctx):
    outs = ctx["run"](
        ("lint", "echo ok", 60),
        ("tests", "printf '\\033[31mboom\\033[0m\\n'; false; echo unreachable", 60),
        ("never", "touch never-ran", 60),
    )
    assert [o["status"] for o in outs] == ["pass", "fail"]
    fail = outs[1]
    assert fail["exit_code"] == 1
    assert fail["detail"] == "exit 1\nboom"  # set -e stopped the script; colour codes are gone
    assert not (ctx["wt"] / "never-ran").exists()


def test_a_nested_sandbox_refusal_is_the_environment_not_the_code(ctx):
    noise = "; ".join(["echo compiling"] * 200)  # the tell-tale line sits far above the tail
    [out] = ctx["run"](("app", f"echo 'sandbox-exec: sandbox_apply: Operation not permitted'; {noise}; exit 65", 60))
    assert out["status"] == "fail" and "sandbox_apply" not in out["detail"]
    assert out["environment"]["line"] == "sandbox-exec: sandbox_apply: Operation not permitted"
    assert "OTHER_SWIFT_FLAGS=$(inherited) -disable-sandbox" in out["environment"]["why"]
    [plain] = ctx["run"](("tests", "echo 'assertion failed'; exit 1", 60))
    assert "environment" not in plain


def test_a_check_cannot_read_credentials_or_inherit_secret_variables(ctx, tmp_path, monkeypatch):
    from ziggy import dispatch

    home = tmp_path / "home"
    (home / ".aws").mkdir(parents=True)
    (home / ".aws" / "credentials").write_text("canary-aws\n")
    (home / ".netrc").write_text("canary-netrc\n")
    (home / "notes.txt").write_text("plain\n")
    monkeypatch.setattr(Path, "home", lambda: home)
    env = dispatch.scrub({**ctx["env"], "GITHUB_TOKEN": "canary-gh", "MY_API_KEY": "canary-key", "LANG": "C"}, keep={"LANG"})
    outs = {}
    for name, cmd in (("aws", f"cat {home}/.aws/credentials"), ("netrc", f"cat {home}/.netrc"), ("plain", f"cat {home}/notes.txt")):
        [outs[name]] = checks.run(ctx["target"], "t1", ctx["wt"], [{"name": name, "run": cmd, "timeout_s": 60}], ctx["log"], env)
    [outs["var"]] = checks.run(ctx["target"], "t1", ctx["wt"],
                               [{"name": "var", "run": 'echo "[${GITHUB_TOKEN:-none}${MY_API_KEY:-}]"', "timeout_s": 60}],
                               ctx["log"], env)
    assert outs["aws"]["status"] == "fail" and "canary" not in outs["aws"]["detail"]
    assert outs["netrc"]["status"] == "fail" and "canary" not in outs["netrc"]["detail"]
    assert outs["plain"]["status"] == "pass" and outs["plain"]["detail"] == "plain"
    assert outs["var"]["detail"] == "[none]"  # scrubbed before the check ever ran


def test_pipefail_fails_a_pipeline(ctx):
    [out] = ctx["run"](("tests", "false | cat", 60))
    assert out["status"] == "fail" and out["detail"].startswith("exit 1")


def test_detail_tail_is_capped(ctx):
    [out] = ctx["run"](("tests", "for i in $(seq 1 2000); do echo xxxxxxxxxx$i; done; exit 3", 60))
    how, tail = out["detail"].split("\n", 1)
    assert how == "exit 3" and out["exit_code"] == 3
    assert len(tail) <= checks.DETAIL_MAX and tail.endswith("xxxxxxxxxx2000")


def test_log_framing(ctx):
    ctx["run"](("lint", "echo hello", 60), ("tests", "exit 2", 60))
    text = ctx["log"].read_text()
    iso = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z"
    wt = re.escape(str(ctx["wt"]))
    assert re.fullmatch(
        rf'\n=== check "lint" · {iso} · cwd {wt}\n\$ echo hello\nhello\n=== lint: pass \(exit 0, \d+s\)\n'
        rf'\n=== check "tests" · {iso} · cwd {wt}\n\$ exit 2\n=== tests: fail \(exit 2, \d+s\)\n',
        text,
    ), text


def test_timeout_kills_the_whole_process_group(ctx):
    # The background child would create `late` after 2s if only bash were killed.
    [out] = ctx["run"](("tests", "(sleep 2; touch late) & sleep 30", 1))
    assert out["status"] == "fail" and out["exit_code"] is None
    assert out["detail"].startswith("timed out after 1s")
    assert out["duration_ms"] < 10_000
    time.sleep(2.5)
    assert not (ctx["wt"] / "late").exists()
    assert "=== tests: fail (timed out after 1s, 1s)\n" in ctx["log"].read_text()


def test_stop_ends_the_check(ctx):
    pid_path = ctx["log"].parent / PID_FILE

    def stopper():
        deadline = time.monotonic() + 10
        while not pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        (ctx["log"].parent / STOP_FILE).write_text("now\n")
        os.killpg(int(pid_path.read_text()), signal.SIGTERM)

    thread = threading.Thread(target=stopper)
    thread.start()
    [out] = ctx["run"](("tests", "sleep 30", 60))
    thread.join()
    assert out["status"] == "fail" and out["detail"].startswith("stopped")
    assert not pid_path.exists()


def test_env_carries_check_name_and_run_env(ctx):
    [out] = ctx["run"](("unit tests", 'echo "$PIPELINE_CHECK/$ZIGGY_RUN"', 60))
    assert out["status"] == "pass" and out["detail"] == "unit tests/1"


@pytest.fixture
def git_ctx(ctx):
    wt = ctx["wt"]
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=wt, check=True)
    (wt / ".gitignore").write_text("build/\n")
    (wt / "app.py").write_text("x = 1\n")
    (wt / "keep").mkdir()
    (wt / "keep" / "old.txt").write_text("old\n")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=wt, check=True)
    return ctx


def test_files_a_check_leaves_behind_are_removed_with_a_warning(git_ctx):
    wt = git_ctx["wt"]
    (wt / "mine.txt").write_text("there before\n")  # untracked before the check: not the check's doing
    outs = git_ctx["run"](
        ("tests", "touch ':memory:.ses'; mkdir -p new/deep build; touch new/deep/x keep/y build/out; echo ok", 60),
        ("lint", "echo clean", 60),
    )
    assert [o["status"] for o in outs] == ["pass", "pass"]
    assert outs[0]["left_files"] == [":memory:.ses", "keep/y", "new/deep/x"]
    assert outs[0]["detail"].startswith("warning: removed files the check left behind: :memory:.ses, keep/y, new/deep/x\n")
    assert "left_files" not in outs[1]
    assert not (wt / ":memory:.ses").exists() and not (wt / "new").exists() and not (wt / "keep" / "y").exists()
    assert (wt / "keep" / "old.txt").exists() and (wt / "mine.txt").exists() and (wt / "build" / "out").exists()
    assert "=== tests: warning: removed files the check left behind: " in git_ctx["log"].read_text()


def test_a_check_that_modifies_tracked_files_fails_and_they_stay(git_ctx):
    wt = git_ctx["wt"]
    outs = git_ctx["run"](("format", "echo 'x = 2' > app.py; rm keep/old.txt", 60), ("never", "touch never-ran", 60))
    assert [o["status"] for o in outs] == ["fail"]
    assert outs[0]["detail"].startswith("checks modified tracked files: app.py, keep/old.txt")
    assert (wt / "app.py").read_text() == "x = 2\n"
    assert "=== format: checks modified tracked files: app.py, keep/old.txt\n" in git_ctx["log"].read_text()


def test_missing_sandbox_exec_never_runs_unsandboxed(any_ctx, monkeypatch):
    monkeypatch.setattr(checks, "SANDBOX_EXEC", "/nonexistent/sandbox-exec")
    monkeypatch.setattr(checks, "BWRAP", "/nonexistent/bwrap")
    [out] = any_ctx["run"](("tests", "touch ran", 60))
    assert not (any_ctx["wt"] / "ran").exists()
    assert out["status"] == "fail" and out["exit_code"] is None
    assert out["detail"] == "could not start: no sandbox available (sandbox-exec on macOS, bwrap on Linux)"


def test_missing_worktree_could_not_start(ctx):
    ctx["wt"].rmdir()
    [out] = ctx["run"](("tests", "true", 60))
    assert out["detail"] == f"could not start: working directory not found: {ctx['wt']}"


def test_network_is_denied_except_loopback(ctx):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    host_loopback = checks.backend() == "sandbox-exec"  # bwrap: a network namespace of its own, its own loopback
    try:
        [out] = ctx["run"](("loopback", f"exec 3<>/dev/tcp/127.0.0.1/{port}; echo connected", 60))
        assert (out["status"] == "pass" and out["detail"] == "connected") if host_loopback else out["status"] == "fail"
    finally:
        server.close()
    inside = ("python3 -c \"import socket; s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(); "
              "c = socket.create_connection(s.getsockname()); print('connected')\"")
    [out] = ctx["run"](("own-loopback", inside, 60))
    assert out["status"] == "pass" and out["detail"] == "connected"
    [out] = ctx["run"](("egress", "exec 3<>/dev/tcp/1.1.1.1/80; echo connected", 60))
    assert out["status"] == "fail" and "connected" not in out["detail"]


def test_writes_outside_the_allowed_roots_are_denied(ctx):
    outside = Path.home() / f".ziggy-check-{uuid.uuid4().hex}"
    state_file = ctx["target"].state_dir / "venv-marker"
    try:
        [out] = ctx["run"](("writes", f"touch in-worktree '{state_file}' \"${{TMPDIR:-/tmp}}/tmp-marker\" /tmp/x-$$; rm /tmp/x-$$", 60))
        assert out["status"] == "pass", out["detail"]
        assert (ctx["wt"] / "in-worktree").exists() and state_file.exists()
        [out] = ctx["run"](("outside", f"touch '{outside}'", 60))
        assert out["status"] == "fail" and ("Operation not permitted" in out["detail"] or "Read-only file system" in out["detail"])
        assert not outside.exists()
    finally:
        outside.unlink(missing_ok=True)


def test_profile_escapes_paths(tmp_path):
    text = checks.profile(tmp_path / 'a "b" \\c', tmp_path / "state")
    assert '/a \\"b\\" \\\\c")' in text
    with pytest.raises(ValueError):
        checks.profile(tmp_path / "bad\nname", tmp_path / "state")


def test_profile_adds_a_preset_only_when_asked(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    plain = checks.profile(tmp_path / "wt", tmp_path / "state")
    xcode = checks.profile(tmp_path / "wt", tmp_path / "state", ("xcode",))
    swiftpm = os.path.realpath(tmp_path / "home" / "Library" / "Caches" / "org.swift.swiftpm")
    rules = [f'(allow file-write* (subpath "{swiftpm}"))', '(allow network-outbound (remote unix-socket (subpath "/private/var/tmp")))']
    assert all(r not in plain for r in rules) and "swiftpm" not in plain and "/private/var/tmp" not in plain
    # The preset's rules go right before the credential read denies, which stay last (the last matching rule wins).
    deny = checks._read_denies()[0]
    assert plain.rstrip("\n").endswith(deny) and xcode.rstrip("\n").endswith("\n".join(rules) + "\n" + deny)
    assert xcode.replace("\n".join(rules) + "\n", "") == plain
    with pytest.raises(ValueError, match="unknown check_sandbox preset 'docker'"):
        checks.profile(tmp_path / "wt", tmp_path / "state", ("docker",))


def test_an_unknown_preset_never_runs_the_check(any_ctx):
    target = config.Target(name="demo", path=any_ctx["target"].path, check_sandbox=("docker",))
    [out] = checks.run(target, "t1", any_ctx["wt"], [{"name": "t", "run": "touch ran", "timeout_s": 60}], any_ctx["log"], any_ctx["env"])
    assert out["status"] == "fail" and not (any_ctx["wt"] / "ran").exists()
    assert "could not start" in out["detail"]


def test_check_sandbox_is_validated_in_the_config(env):
    text = env["config"].read_text()
    env["config"].write_text(text.replace("prepare = [", 'check_sandbox = ["xcode"]\nprepare = ['))
    assert config.load_target("demo").check_sandbox == ("xcode",)
    env["config"].write_text(text)
    assert config.load_target("demo").check_sandbox == ()
    env["config"].write_text(text.replace("prepare = [", 'check_sandbox = ["xcode", "docker"]\nprepare = ['))
    with pytest.raises(config.ConfigError, match="unknown preset 'docker'"):
        config.load_target("demo")
    env["config"].write_text(text.replace("prepare = [", 'check_sandbox = "xcode"\nprepare = ['))
    with pytest.raises(config.ConfigError, match="must be a list"):
        config.load_target("demo")


def test_short_duration():
    assert [checks.short_duration(s) for s in (0, 12.4, 59.6, 180, 3900, 2 * 86400 + 4 * 3600)] == \
        ["0s", "12s", "60s", "3m", "1h 05m", "2d 4h"]


def test_the_linux_sandbox_binds_only_the_worktree_and_state_writable_and_hides_credentials(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".netrc").write_text("canary\n")
    monkeypatch.setattr(Path, "home", lambda: home)
    wt, state_dir = tmp_path / "wt", tmp_path / "state"
    wt.mkdir()
    deny = tmp_path / "deny"
    deny.write_text("")
    deny.chmod(0)
    argv = checks.bwrap_argv(wt, state_dir, "pytest -q", deny)
    pairs = list(zip(argv, argv[1:]))
    assert argv[1:4] == ["--ro-bind", "/", "/"] and ("--unshare-net", "--die-with-parent") in pairs
    binds = [argv[i + 1] for i, a in enumerate(argv) if a == "--bind"]
    assert binds == ["/tmp", str(wt.resolve()), str(state_dir.resolve())]
    assert ("--tmpfs", os.path.realpath(home / ".ssh")) in pairs
    i = argv.index(os.path.realpath(home / ".netrc"))
    assert argv[i - 2:i] == ["--ro-bind", str(deny)] and not (state_dir / ".sandbox-deny").exists()
    assert argv[-3:] == ["bash", "-c", "set -eo pipefail\npytest -q"] and ("--chdir", str(wt.resolve())) in pairs


def test_the_scrub_drops_secrets_but_keeps_git_identity():
    from ziggy import dispatch
    env = {"GIT_AUTHOR_NAME": "Ada", "GIT_AUTHOR_EMAIL": "ada@example.test", "GIT_COMMITTER_NAME": "Ada",
           "NPM_AUTH_TOKEN": "x", "OAUTH_CLIENT": "x", "BASIC_AUTH": "x", "MY_SESSION_ID": "x", "PATH": "/bin"}
    assert set(dispatch.scrub(env, keep=set())) == {"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "PATH"}
