"""How hard the machine is working: macOS memory pressure, swap and load, sampled cheaply and cached a few seconds.

A Mac that swaps hard turns seconds into minutes: a stage started then spends its timeout waiting for `cat` and `grep`.
So, like a Claude login over its usage limit (quota.py), a busy machine holds new stages: before a stage starts, `busy`
(memory pressure critical, or swap used at `[host] swap_hold` (0.9) of its total or more) keeps the ticket in status
`host` with the step it takes in `next`; the panel's tick (`wake`) starts it once the machine has been calm for two
ticks in a row, one ticket per tick, oldest first. `ziggy run <id> --now` starts it anyway, with a warning. A running
stage is never stopped for it. Every stage records a sample in run.json (`host`: start, end and the worst seen).

`sample()` never raises: `sysctl` that fails or is missing (not a Mac) gives pressure `unknown` and no swap figures.
ZIGGY_HOST_SAMPLE (tests) names a file holding what `sysctl -n kern.memorystatus_vm_pressure_level vm.swapusage`
prints, read instead of running it (no cache then).
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import config, state
from .config import Target

STATUS = "host"  # the ticket status while it waits for the machine to have memory again
CACHE_S = 5.0  # a sample is reused this long
CALM_TICKS = 2  # panel ticks in a row the machine must be calm before a held ticket starts
SHOW_SWAP = 0.75  # swap used share worth naming in texts; on its own (normal pressure) it shows nothing: macOS keeps swap full
FIXTURE_ENV = "ZIGGY_HOST_SAMPLE"
NAMES = ("kern.memorystatus_vm_pressure_level", "vm.swapusage")
LEVELS = {1: "normal", 2: "warn", 4: "critical"}  # kern.memorystatus_vm_pressure_level
WORDS = {"normal": "normal", "warn": "baskı altında", "critical": "kritik", "unknown": "bilinmiyor"}
RANK = {"unknown": 0, "normal": 1, "warn": 2, "critical": 3}
_SWAP = re.compile(r"\b(total|used|free)\s*=\s*([\d.,]+)\s*([KMGT]?)", re.I)
_UNIT = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}

_cache: tuple[float, dict] | None = None
_calm = 0  # the panel's consecutive calm ticks (wake)


def parse(text: str, load1: float | None = None, at: str | None = None) -> dict:
    """A sample from sysctl's output: the pressure level line (1/2/4) and the vm.swapusage line
    (`total = 26624.00M  used = 25576.44M  free = 1047.56M  (encrypted)`; a decimal comma is read too)."""
    pressure, swap = "unknown", {}
    for line in (text or "").splitlines():
        line = line.strip()
        if line.isdigit():
            pressure = LEVELS.get(int(line), "unknown")
            continue
        for key, number, unit in _SWAP.findall(line):
            try:
                swap[key.lower()] = int(float(number.replace(",", ".")) * _UNIT[unit.upper()])
            except ValueError:
                pass
    return {"pressure": pressure, "swap_used": swap.get("used"), "swap_total": swap.get("total"),
            "load1": None if load1 is None else round(load1, 2), "at": at or state.now()}


def _read() -> str:
    fixture = os.environ.get(FIXTURE_ENV)
    if fixture:
        return Path(fixture).read_text(encoding="utf-8")
    return subprocess.run(["sysctl", "-n", *NAMES], capture_output=True, text=True, timeout=5,
                          env={**os.environ, "LC_ALL": "C"}).stdout


def sample(fresh: bool = False) -> dict:
    """{pressure: normal | warn | critical | unknown, swap_used, swap_total (bytes or None), load1, at}."""
    global _cache
    fixture = bool(os.environ.get(FIXTURE_ENV))
    if not fresh and not fixture and _cache and time.monotonic() - _cache[0] < CACHE_S:
        return _cache[1]
    try:
        text = _read()
    except (OSError, subprocess.SubprocessError, ValueError):
        text = ""
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = None
    out = parse(text, load1)
    if not fixture:
        _cache = (time.monotonic(), out)
    return out


def swap_share(s: dict) -> float | None:
    total, used = s.get("swap_total"), s.get("swap_used")
    return used / total if total and used is not None else None


def busy(s: dict, swap_hold: float = 0.9) -> bool:
    """No stage starts: memory pressure is critical, or it is raised (warn) with swap used at `swap_hold` of its total
    or more. Full swap alone is not strain: macOS pages swap back in only when it is touched, so it stays near full
    for hours after the memory was freed (18 Oct: swap 93 % at normal pressure held every stage)."""
    share = swap_share(s)
    if s.get("pressure") == "critical":
        return True
    return s.get("pressure") == "warn" and share is not None and share >= swap_hold


def worst(a: dict | None, b: dict | None) -> dict | None:
    """The sample that shows the machine under more strain (pressure, then swap share)."""
    if not a or not b:
        return a or b
    key = lambda s: (RANK.get(s.get("pressure"), 0), swap_share(s) or 0)  # noqa: E731
    return b if key(b) > key(a) else a


def text(s: dict) -> str:
    """`Bellek: kritik · swap %96` (the panel's chip and the hold line)."""
    share = swap_share(s)
    return f"Bellek: {WORDS.get(s.get('pressure'), s.get('pressure'))}" + (f" · swap %{round(share * 100)}" if share is not None else "")


def suffix(s: dict) -> str:
    """What a stall item adds about the machine: `bellek kritik · swap %96`, '' when it is fine."""
    share = swap_share(s)
    parts = [f"bellek {WORDS[s['pressure']]}"] if s.get("pressure") in ("warn", "critical") else []
    if share is not None and share >= SHOW_SWAP:
        parts.append(f"swap %{round(share * 100)}")
    return " · ".join(parts)


def phrase(s: dict | None) -> str:
    """How a stage's worst sample reads at the end of a sentence: `bellek baskısı kritikti (swap %96)`, '' unknown."""
    if not s or s.get("pressure") == "unknown" and swap_share(s) is None:
        return ""
    share = swap_share(s)
    swap = f" (swap %{round(share * 100)})" if share is not None and share >= SHOW_SWAP else ""
    said = {"critical": "bellek baskısı kritikti", "warn": "bellek baskı altındaydı"}.get(s.get("pressure"))
    if said:
        return said + swap
    return f"swap %{round(share * 100)} doluydu" if swap else "makinenin belleği rahattı"


def cli_text(s: dict, cfg: config.HostConfig) -> str:
    """`memory critical · swap 96% (25.0 of 26.0 GB) · load 16.20 · new stages wait` (`ziggy status`)."""
    share = swap_share(s)
    parts = [f"memory {s.get('pressure')}"]
    if share is not None:
        parts.append(f"swap {round(share * 100)}% ({s['swap_used'] / 1024 ** 3:.1f} of {s['swap_total'] / 1024 ** 3:.1f} GB)")
    if s.get("load1") is not None:
        parts.append(f"load {s['load1']:.2f}")
    if cfg.enabled and busy(s, cfg.swap_hold):
        parts.append("new stages wait")
    return " · ".join(parts)


def view() -> dict:
    """The panel's chip: {pressure, swap_percent, load1, busy, tone, show, text}; `show` while pressure is warn or
    critical (tone bad while it holds stages, else warn). A full swap at normal pressure is not shown: see `busy`."""
    s = sample()
    try:
        cfg = config.load_host()
    except config.ConfigError:
        cfg = config.HostConfig()
    share = swap_share(s)
    hold = cfg.enabled and busy(s, cfg.swap_hold)
    show = s["pressure"] in ("warn", "critical")
    return {"pressure": s["pressure"], "swap_percent": None if share is None else round(share * 100), "load1": s["load1"],
            "busy": hold, "enabled": cfg.enabled, "show": show, "tone": "bad" if hold else "warn" if show else "ok",
            "text": text(s)}


# --- tickets held while the machine is busy ------------------------------------------------------------

def held(st: dict, step: dict, *, s: dict) -> None:
    """Put a ticket in the `host` status: `next` is the step it takes once the machine is calm again."""
    st.update(status=STATUS, next=step, host={"stage": step["stage"], "since": state.now(), "sample": s})


def wake(targets: list[Target]) -> list[str]:
    """The panel's tick: count calm ticks (any busy tick starts over) and, from the CALM_TICKS-th on, start the ticket
    held longest, one per tick, as a detached `ziggy run` job. Returns the ids started."""
    global _calm
    cfg = config.load_host()
    if cfg.enabled and busy(sample(fresh=True), cfg.swap_hold):
        _calm = 0
        return []
    _calm += 1
    if _calm < CALM_TICKS:
        return []
    waiting = []
    for target in targets:
        for ticket_id in state.ticket_ids(target):
            try:
                st = state.load_state(target, ticket_id)
            except (OSError, ValueError):
                continue
            if st.get("status") == STATUS:
                waiting.append(((st.get("host") or {}).get("since") or "", target, ticket_id))
    for _, target, ticket_id in sorted(waiting, key=lambda w: w[0]):
        try:
            with state.lock(target, f"wake-{ticket_id}", f"start of {ticket_id} once the machine is calm"):
                from . import dashboard  # the panel's jobs: dashboard imports the engine
                if state.load_state(target, ticket_id).get("status") != STATUS or dashboard._busy(target, ticket_id):
                    continue
                dashboard.start_job(target, ticket_id, "run", ticket_id)
        except state.LockError:
            continue
        except (OSError, ValueError, LookupError) as exc:
            print(f"{ticket_id}: could not start once the machine was calm: {exc}", file=sys.stderr)
            continue
        print(f"{ticket_id}: the machine has memory again; started", file=sys.stderr, flush=True)
        return [ticket_id]
    return []
