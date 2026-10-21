"""The one shared-metrics consent answer, and the terminal's first-run offer.

Every surface reads and writes the same two keys in the profile's config.yaml:
``telemetry.shared_metrics.enabled`` (collect) and ``.send`` (upload daily). The offer has two
answers, and each sets both keys: "Share with Nous" turns both on, "No thanks" turns both off. The
question counts as answered once either key is written explicitly; the shipped defaults are not an
answer. Every answer also writes ``offer_version``, and two older answers are asked once more, with
the reason, where any response (Esc or dismiss included) settles it for good:
- a "No thanks" without ``offer_version`` predates the type-ahead fix (an Enter pressed during
  startup could save it unseen);
- a "keep it on this computer" answer (collect, never send) from before version 3, when the offer
  dropped that answer. Esc or dismiss on this re-ask turns collection off.
Desktop paints the offer as a composer strip (``consent-strip.tsx`` over the ``shared_metrics.*``
RPCs). The terminal asks at the end of every ``hermes setup`` flow and, for a profile that never
answered, once before an interactive ``hermes`` / ``hermes --tui`` chat (which also covers the
dashboard's Chat tab, a PTY-hosted TUI). The messaging gateway never asks: a chat participant is
not the install owner.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)

# (label, enabled, send): the offer's two answers, in the order every surface shows them.
OFFER_CHOICES = (
    ("Share with Nous", True, True),
    ("No thanks", False, False),
)
_NO_THANKS = len(OFFER_CHOICES) - 1
DOCS_URL = "https://hermes-agent.nousresearch.com/docs/developer-guide/relay-shared-metrics"
_OFFER_DESCRIPTION = "\n".join((
    "Shared metrics are bounded counters: activity, outcomes, error classes (with a",
    "fixed-list reason when a memory write, compression, update or install fails),",
    "model routes, token totals, feature use and coarse machine facts. Never prompts,",
    "files, paths, setting values or error text. Sharing sends them to Nous once a",
    "day. Apart from a one-time install note, data from before you opt in is never",
    "sent.",
    f"Details: {DOCS_URL}",
    "Change it any time: hermes setup telemetry",
))
_REASK_NOTE = "Asking once more: an earlier version could save \"No thanks\" before you saw this question."
_REASK_LOCAL_NOTE = "Asking once more: Hermes no longer keeps shared metrics only on this machine."


def _section(cfg: Any) -> dict:
    telemetry = cfg.get("telemetry") if isinstance(cfg, dict) else None
    section = telemetry.get("shared_metrics") if isinstance(telemetry, dict) else None
    return section if isinstance(section, dict) else {}


# Written with every answer. 2 = after the type-ahead fix; 3 = after the offer dropped the
# collect-only answer.
OFFER_VERSION = 3


def _offer_version(section: dict) -> int:
    version = section.get("offer_version")
    return version if isinstance(version, int) and not isinstance(version, bool) else 0


def consent_state(raw_cfg: Any) -> dict:
    """``{enabled, send, decided, reask}`` from a RAW config mapping (no defaults merged). ``reask``
    reads undecided so every surface offers it once more: an "off" answer from before version 2
    that may never have been seen (``enabled`` false), or a collect-only answer from before version
    3 (``enabled`` true), which the offer no longer has. A collect-only answer written at version 3
    came from an older client and stays. An opt-in to sending was always a deliberate pick and stays."""
    section = _section(raw_cfg)
    enabled = section.get("enabled") is True
    send = enabled and section.get("send") is True
    answered = "enabled" in section or "send" in section
    version = _offer_version(section)
    reask = answered and ((not enabled and version < 2) or (enabled and not send and version < 3))
    return {
        "enabled": enabled,
        "send": send,
        "decided": answered and not reask,
        "reask": reask,
    }


def _current_state() -> dict:
    from hermes_cli.config import read_raw_config

    return consent_state(read_raw_config())


def set_answer(target: dict, enabled: bool, send: bool) -> None:
    """Write one answer into a config mapping: both opt-ins plus the offer version that settles it."""
    telemetry = target.get("telemetry")
    if not isinstance(telemetry, dict):
        telemetry = target["telemetry"] = {}
    section = telemetry.get("shared_metrics")
    if not isinstance(section, dict):
        section = telemetry["shared_metrics"] = {}
    section["enabled"], section["send"] = enabled, enabled and send
    section["offer_version"] = OFFER_VERSION


def save_consent(enabled: bool, send: bool, config: dict | None = None) -> None:
    """Write both keys into the user's raw config.yaml now. ``save_config`` strips values equal
    to the shipped defaults, so a "No thanks" (both false) saved through it vanished and the
    profile was asked again on every surface. ``config`` (a caller's in-memory copy, e.g. the
    wizard's) gets the same answer so its later save agrees. Sending cannot outlive collection."""
    from hermes_cli.config import (
        _write_user_config, get_config_path, is_managed, managed_error, require_readable_config_before_write,
    )
    from hermes_cli.setup import _record_send_consent_change

    if is_managed():
        managed_error("save configuration")
        return
    send = enabled and send
    config_path = get_config_path()
    user_config = require_readable_config_before_write(config_path)
    set_answer(user_config, enabled, send)
    _write_user_config(config_path, user_config)
    if config is not None:
        set_answer(config, enabled, send)
    # Unconditional: a send key already false may still have an open consent window.
    _record_send_consent_change(enabled=send)
    if not enabled:
        from hermes_constants import get_hermes_home

        from .shared_metrics_process import purge_pending_receipts

        purge_pending_receipts(get_hermes_home())


def _reask_reason(state: dict) -> str | None:
    """Which older answer a re-ask covers: ``"local"`` (collect-only, enabled) or ``"off"``."""
    if not state.get("reask"):
        return None
    return "local" if state.get("enabled") else "off"


def offer_consent(config: dict | None = None, *, reask: bool | str | None = None) -> bool:
    """Ask once, with the offer's two answers; "No thanks" is the default so Enter never opts
    anyone in. Esc leaves a first question open (asked again next time). On a re-ask (``"off"``, or
    True, for an unseen "No thanks"; ``"local"`` for a retired collect-only answer) Esc saves "No
    thanks", so nobody is asked a third time. True when answered."""
    from hermes_cli.cli_output import print_info, print_success
    from hermes_cli.curses_ui import curses_radiolist, flush_stdin

    reask = "off" if reask is True else reask or None
    note = {"off": _REASK_NOTE, "local": _REASK_LOCAL_NOTE}.get(reask)
    # The offer appears seconds into startup; an Enter typed while Hermes booted would otherwise
    # answer it ("No thanks") before it was ever on screen, so the user was never really asked.
    flush_stdin()
    idx = curses_radiolist(
        "Help improve Hermes?", [label for label, _, _ in OFFER_CHOICES], selected=_NO_THANKS, cancel_returns=-1,
        description=f"{note}\n{_OFFER_DESCRIPTION}" if note else _OFFER_DESCRIPTION,
    )
    if idx < 0 and reask:
        save_consent(False, False, config)
        kept = "Kept \"No thanks\"." if reask == "off" else "Shared metrics off."
        print_info(f"{kept} Change it any time with `hermes setup telemetry`.")
        return True
    if idx < 0:
        print_info("Not answered; Hermes will ask again. Decide any time with `hermes setup telemetry`.")
        return False
    _, enabled, send = OFFER_CHOICES[idx]
    save_consent(enabled, send, config)
    outcome = "sent to Nous once a day" if send else "off"
    print_success(f"Shared metrics {outcome}. Change it any time with `hermes setup telemetry`.")
    return True


def offer_consent_if_undecided(config: dict | None = None) -> None:
    """The end-of-setup offer: every ``hermes setup`` flow funnels through the setup-completed
    record, so this one call covers Quick, Full, Blank Slate, Portal and ``--quick``."""
    from hermes_cli.config import is_managed
    from hermes_cli.setup import is_noninteractive

    if is_managed() or is_noninteractive() or not sys.stdin.isatty():
        return
    state = _current_state()
    if state["decided"]:
        return
    try:
        offer_consent(config, reask=_reask_reason(state))
    except (KeyboardInterrupt, EOFError):
        print()


def offer_consent_before_chat(args: Any) -> None:
    """One-time offer before an interactive chat on a profile that never answered. Skipped for
    anything without a person at a terminal (``-q``, piped or JSON output, spawned actions) and for
    a Desktop-spawned pane (the app shows its own strip)."""
    from hermes_cli.config import is_managed
    from hermes_cli.setup import is_noninteractive

    if any(getattr(args, name, None) for name in ("query", "query_file", "oneshot", "oneshot_exit")):
        return
    if getattr(args, "safe_mode", False) or getattr(args, "ignore_user_config", False):
        return
    if os.environ.get("HERMES_DESKTOP") or is_noninteractive():
        return
    if not (sys.stdin.isatty() and sys.stdout.isatty()) or is_managed():
        return
    try:
        state = _current_state()
        if state["decided"]:
            return
        offer_consent(reask=_reask_reason(state))
    except (KeyboardInterrupt, EOFError):
        print()
    except Exception:
        logger.debug("shared-metrics offer skipped", exc_info=True)
