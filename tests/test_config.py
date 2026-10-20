"""Config validation for preserve, teardown and ports, and the [notifications] and [chat] tables."""
import pytest

from ziggy import config
from ziggy.config import ConfigError


def write(env, demo="", other=None, top=""):
    text = top + f'[targets.demo]\npath = "{env["repo"]}"\n' + demo
    if other is not None:
        text += f'[targets.other]\npath = "{env["repo"]}"\n' + other
    env["config"].write_text(text)


def test_new_target_keys_load(env):
    write(env, 'preserve = [".env", ".claude/**"]\nteardown = ["make stop"]\nports = { start = 4100, size = 10 }\n')
    t = config.load_target("demo")
    assert t.preserve == (".env", ".claude/**") and t.teardown == ("make stop",)
    assert t.ports == {"start": 4100, "size": 10, "count": 100}


def test_defaults_without_the_keys(env):
    write(env)
    t = config.load_target("demo")
    assert t.preserve == () and t.teardown == () and t.ports is None


@pytest.mark.parametrize("glob", ["../secrets", "a/../../b", "/etc/passwd", "~/x", "", ".git/config"])
def test_preserve_must_stay_in_the_repository(env, glob):
    write(env, f"preserve = [{glob!r}]\n".replace("'", '"'))
    with pytest.raises(ConfigError, match="relative to the repository, without '..'"):
        config.load_target("demo")


def test_teardown_must_be_commands(env):
    write(env, 'teardown = "make stop"\n')
    with pytest.raises(ConfigError, match="teardown must be a list of shell commands"):
        config.load_target("demo")


@pytest.mark.parametrize("ports, error", [
    ("{ start = 80, size = 10 }", "ports.start must be a whole number from 1024"),
    ("{ start = 4100, size = 0 }", "ports.size must be"),
    ('{ start = 4100, size = "10" }', "ports.size must be"),
    ("{ start = 4100, size = true }", "ports.size must be"),
    ("{ start = 65000, size = 100, count = 10 }", "goes past port 65535"),
    ("{ start = 4100, base = 1 }", "unknown keys in \\[targets.demo\\] ports: base"),
    ("4100", "ports must be a table"),
])
def test_ports_validation(env, ports, error):
    write(env, f"ports = {ports}\n")
    with pytest.raises(ConfigError, match=error):
        config.load_target("demo")


def test_port_ranges_must_not_overlap_across_targets(env):
    write(env, "ports = { start = 4100, size = 10, count = 10 }\n", "ports = { start = 4199, size = 5, count = 2 }\n")
    with pytest.raises(ConfigError, match=r"ports 4100-4199 overlap \[targets.other\] ports 4199-4208"):
        config.load_target("demo")
    write(env, "ports = { start = 4100, size = 10, count = 10 }\n", "ports = { start = 4200, size = 5, count = 2 }\n")
    assert config.load_target("demo").ports["count"] == 10 and config.load_target("other").ports["start"] == 4200


def test_notifications_defaults_and_values(env, tmp_path, monkeypatch):
    write(env)
    assert config.load_notifications() == config.NotificationsConfig()
    assert config.load_notifications().events == config.NOTIFY_EVENTS and config.load_notifications().min_interval == 30
    write(env, top='[notifications]\nenabled = false\nevents = ["question", "failed"]\nmin_interval = "2m"\n')
    n = config.load_notifications()
    assert n.enabled is False and n.events == ("question", "failed") and n.min_interval == 120
    monkeypatch.setenv("ZIGGY_CONFIG", str(tmp_path / "missing.toml"))
    assert config.load_notifications() == config.NotificationsConfig()


@pytest.mark.parametrize("table, error", [
    ('enabled = "yes"', "enabled must be true or false"),
    ('events = ["question", "lunch"]', "unknown events lunch"),
    ('min_interval = "soon"', "min_interval: invalid duration"),
    ("tool = 'auto'", "unknown keys in \\[notifications\\]: tool"),
])
def test_notifications_validation(env, table, error):
    write(env, top=f"[notifications]\n{table}\n")
    with pytest.raises(ConfigError, match=error):
        config.load_notifications()


def test_chat_defaults_values_and_validation(env):
    write(env)
    assert config.load_chat() == config.ChatConfig(models=("sonnet", "opus", "fable", "haiku"), timeout=1800)
    write(env, top='[chat]\nmodels = ["sonnet", "opus"]\ntimeout = "10m"\n')
    assert config.load_chat() == config.ChatConfig(models=("sonnet", "opus"), timeout=600)
    for bad in ("models = []", 'models = ["--dangerously-skip-permissions"]', 'timeout = "0s"', "speed = 1"):
        write(env, top=f"[chat]\n{bad}\n")
        with pytest.raises(ConfigError):
            config.load_chat()


def test_the_example_config_parses_with_the_new_keys():
    import tomllib

    data = tomllib.loads((config.PIPELINE_ROOT / "config.example.toml").read_text())
    example = data["targets"]["example"]
    assert config._preserve("x", "example", example["preserve"]) and config._ports("x", "example", example["ports"])
    assert set(data["notifications"]) <= config._NOTIFY_KEYS and set(data["notifications"]["events"]) <= set(config.NOTIFY_EVENTS)
    assert set(data["chat"]) <= config._CHAT_KEYS
