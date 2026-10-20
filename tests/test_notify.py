"""notify: argv building (osascript quoting, terminal-notifier option safety), the persisted rate limit with
its summary, and that nothing ever raises or reaches a real notifier when it is off."""
import subprocess

import pytest

from ziggy import config, notify


@pytest.fixture
def shown(tmp_path, monkeypatch):
    """notify with a recording sender, a controllable clock, macOS on and a 30 s window."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("ZIGGY_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.delenv("ZIGGY_NOTIFY", raising=False)
    monkeypatch.setattr(notify, "MACOS", True)
    out = Shown()
    monkeypatch.setattr(notify.time, "time", lambda: out.clock["now"])

    def show(subtitle, message, group):
        out.append((subtitle, message, group))
        return True

    monkeypatch.setattr(notify, "_show", show)
    return out


class Shown(list):
    """The notifications put on screen, and the clock they see."""

    def __init__(self):
        super().__init__()
        self.clock = {"now": 1_000_000.0}


def test_osascript_argv_quotes_backslashes_and_quotes_and_flattens_lines():
    argv = notify.argv("/usr/bin/osascript", 'dmo-1 · "demo"', 'say \\"hi"\n\tnow', "g")
    assert argv == ["/usr/bin/osascript", "-e",
                    'display notification "say \\\\\\"hi\\" now" with title "Ziggy" subtitle "dmo-1 · \\"demo\\""']
    assert notify.argv("/usr/bin/osascript", "", "x", "g")[2] == 'display notification "x" with title "Ziggy"'


def test_osascript_argv_survives_applescript_injection_attempts():
    # A title that tries to close the string and run more AppleScript stays one literal.
    argv = notify.argv("osascript", "", '" & (do shell script "id") & "', "g")
    assert argv[2] == 'display notification "\\" & (do shell script \\"id\\") & \\"" with title "Ziggy"'


def test_terminal_notifier_argv_keeps_values_from_reading_as_options_and_truncates():
    long = "x" * 300
    argv = notify.argv("/opt/homebrew/bin/terminal-notifier", "-sub", "-remove ALL", "demo/dmo-1/failed:1:01-plan")
    assert argv == ["/opt/homebrew/bin/terminal-notifier", "-title", "Ziggy", "-subtitle", " -sub",
                    "-message", " -remove ALL", "-group", "demo/dmo-1/failed:1:01-plan"]
    argv = notify.argv("terminal-notifier", long, long, "g")
    assert argv[4] == "x" * 79 + "…" and argv[6] == "x" * 199 + "…"
    assert "-subtitle" not in notify.argv("terminal-notifier", "  ", "m", "g")


def test_tool_prefers_terminal_notifier_then_homebrew_then_osascript(monkeypatch):
    monkeypatch.setattr(notify.shutil, "which", lambda name: None)
    monkeypatch.setattr(notify.os, "access", lambda path, mode: path == "/usr/local/bin/terminal-notifier")
    assert notify.tool() == "/usr/local/bin/terminal-notifier"
    monkeypatch.setattr(notify.os, "access", lambda path, mode: False)
    assert notify.tool() == notify.OSASCRIPT
    monkeypatch.setattr(notify.shutil, "which", lambda name: "/somewhere/terminal-notifier")
    assert notify.tool() == "/somewhere/terminal-notifier"


def test_rate_limit_counts_what_it_holds_back_and_the_next_one_carries_the_count(shown):
    assert notify.send("dmo-1 · demo", "Koşu başarısız oldu: a", key="k1") is True
    shown.clock["now"] += 10
    assert notify.send("dmo-2 · demo", "b", key="k2") is False
    assert notify.send("dmo-3 · demo", "c", key="k3") is False
    assert len(shown) == 1
    shown.clock["now"] += 25  # 35 s after the first: the window is over
    assert notify.send("dmo-4 · demo", "d", key="k4") is True
    assert shown[-1] == ("dmo-4 · demo", "d (+ 2 öğe daha seni bekliyor)", "k4")
    shown.clock["now"] += 31
    assert notify.send("dmo-5 · demo", "e", key="k5") is True
    assert shown[-1][1] == "e"  # the count was used up


def test_flush_due_shows_the_held_back_count_only_after_the_window(shown):
    assert notify.flush_due() is False  # nothing held back
    notify.send("a", "a", key="a")
    notify.send("b", "b", key="b")
    shown.clock["now"] += 29
    assert notify.flush_due() is False  # the window is still open
    shown.clock["now"] += 2
    assert notify.flush_due() is True
    assert shown[-1] == ("", "1 öğe daha seni bekliyor", notify.SUMMARY_GROUP)
    assert notify.flush_due() is False  # shown once
    assert notify.send("c", "c", key="c") is False  # the summary opened a new window


def test_the_rate_window_lives_in_the_state_file(shown):
    notify.send("a", "a", key="a")
    path = config.state_home() / notify.RATE_FILE
    assert path.is_file()
    path.unlink()  # what another process sees is only the file: without it the window is gone
    assert notify.send("b", "b", key="b") is True
    assert notify.send("c", "c", key="c") is False
    assert notify._load_rate()["suppressed"] == 1


def test_a_failed_show_does_not_use_up_the_window(shown, monkeypatch):
    monkeypatch.setattr(notify, "_show", lambda *a: False)
    assert notify.send("a", "a", key="a") is False
    monkeypatch.setattr(notify, "_show", lambda *a: True)
    assert notify.send("b", "b", key="b") is True


def test_min_interval_and_enabled_come_from_the_config(shown, tmp_path):
    (tmp_path / "config.toml").write_text('[notifications]\nmin_interval = "2m"\n')
    notify.send("a", "a", key="a")
    shown.clock["now"] += 100
    assert notify.send("b", "b", key="b") is False
    (tmp_path / "config.toml").write_text("[notifications]\nenabled = false\n")
    shown.clock["now"] += 1000
    assert notify.send("c", "c", key="c") is False and len(shown) == 1
    (tmp_path / "config.toml").write_text('[notifications]\nevents = ["question"]\n')
    assert notify.wants("question") and not notify.wants("failed")


def test_a_broken_notifications_table_falls_back_to_the_defaults(shown, tmp_path, capsys):
    (tmp_path / "config.toml").write_text('[notifications]\nevents = ["nope"]\n')
    assert notify.send("a", "a", key="a") is True
    assert notify.wants("failed")
    assert "using the defaults" in capsys.readouterr().err


def test_off_by_env_or_platform_never_runs_a_tool(shown, monkeypatch):
    monkeypatch.setenv("ZIGGY_NOTIFY", "off")
    assert notify.send("a", "a", key="a") is False and notify.flush_due() is False
    monkeypatch.delenv("ZIGGY_NOTIFY")
    monkeypatch.setattr(notify, "MACOS", False)
    assert notify.send("a", "a", key="a") is False
    assert shown == []


def test_show_reports_tool_failures_instead_of_raising(monkeypatch, capsys):
    monkeypatch.setattr(notify, "tool", lambda: "/usr/bin/osascript")

    def fail(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "execution error: nope\n")

    monkeypatch.setattr(notify.subprocess, "run", fail)
    assert notify._show("s", "m", "g") is False
    assert "osascript exited 1: execution error: nope" in capsys.readouterr().err

    def hang(cmd, **kw):
        assert kw["timeout"] == notify.TIMEOUT and "shell" not in kw
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(notify.subprocess, "run", hang)
    assert notify._show("s", "m", "g") is False


def test_send_never_raises(shown, monkeypatch):
    def boom(*a):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(notify, "_show", boom)
    assert notify.send("a", "a", key="a") is False
    assert notify.flush_due() is False
