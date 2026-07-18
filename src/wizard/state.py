"""Wizard-state: persistente progress-tracking + config-write.

Elke wizard-stap slaat direct op naar `ROSA_HOME/config.yaml` +
`ROSA_HOME/secrets.env` zodat een crash midden in de setup gewoon
hervat kan worden. De state-file `.wizard_state.json` houdt bij welke
stappen zijn afgerond.

Design-doel: idempotent + hervatbaar. Elke stap is safe om opnieuw te
draaien met dezelfde payload.
"""
from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

# Alle stap-IDs in de volgorde waarin de wizard ze presenteert.
# Skippable stappen zijn expliciet gemarkeerd — verplichte stappen
# moeten completed=True zijn voordat de wizard 'finish' toestaat.
STEP_IDS = (
    "welcome",
    "identity",
    "claude",
    "imessage",
    "google",
    "imap",
    "slack",
    "todoist",
    "plaud",
    "vips",
    "uptime",
    "news",
    "notifications",
    "confidential",
    "features",
    "confirm",
)

REQUIRED_STEPS = ("welcome", "identity", "claude", "confirm")


@dataclass
class WizardState:
    """Progress-tracking over wizard-stappen."""

    completed: set[str]
    skipped: set[str]

    @classmethod
    def load(cls, path: Path) -> "WizardState":
        if not path.exists():
            return cls(completed=set(), skipped=set())
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.exception("wizard-state: kon %s niet lezen", path)
            return cls(completed=set(), skipped=set())
        return cls(
            completed=set(data.get("completed") or []),
            skipped=set(data.get("skipped") or []),
        )

    def save(self, path: Path) -> None:
        payload = {
            "completed": sorted(self.completed),
            "skipped": sorted(self.skipped),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        _chmod_600(path)

    def mark_done(self, step_id: str) -> None:
        self.completed.add(step_id)
        self.skipped.discard(step_id)

    def mark_skipped(self, step_id: str) -> None:
        self.skipped.add(step_id)
        self.completed.discard(step_id)

    def is_finished(self) -> bool:
        return all(s in self.completed for s in REQUIRED_STEPS)


def _chmod_600(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        # L7: op Dropbox/NFS mislukt chmod stil en secrets.env blijft
        # 0644. Loggen zodat de user in de logs kan zien dat de
        # permissions niet klopten en zelf 'chmod 600' kan doen.
        log.warning("chmod 0600 failed on %s: %s", path, exc)


def load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {}
    try:
        return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        log.exception("wizard-state: kon %s niet parsen", config_path)
        return {}


def save_config(config_path: Path, cfg: dict[str, Any]) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False,
                        default_flow_style=False),
        encoding="utf-8",
    )
    tmp.replace(config_path)
    _chmod_600(config_path)


def _deep_merge(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    """Recursief nested dicts mergen — scalars/lists worden vervangen,
    dict-waarden mergen op elk niveau."""
    out = dict(base)
    for k, v in updates.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def update_config(config_path: Path, updates: dict[str, Any]) -> dict[str, Any]:
    """Recursief `updates` in bestaande config.yaml mergen.

    Zie code-review M2: eerdere non-recursieve versie liet een wizard-
    re-run stille dataloss doen op nested keys (bv. briefings.enabled
    weg-overschreven door alleen morning_time).
    """
    cfg = load_config(config_path)
    merged = _deep_merge(cfg, updates)
    save_config(config_path, merged)
    return merged


def load_secrets(secrets_path: Path) -> dict[str, str]:
    """Leest bestaande secrets.env (KEY=VALUE per regel)."""
    if not secrets_path.exists():
        return {}
    result: dict[str, str] = {}
    for line in secrets_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        result[k.strip()] = v.strip().strip('"').strip("'")
    return result


def save_secret(secrets_path: Path, key: str, value: str) -> None:
    """Schrijf één KEY=VALUE naar secrets.env, respect bestaande sleutels.

    File-mode blijft 0600 — deze file bevat API-keys en OAuth tokens.
    """
    existing = load_secrets(secrets_path)
    existing[key] = value
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Rosa secrets — gegenereerd door de setup-wizard.",
        "# Bewerk handmatig alleen als je weet wat je doet.",
        "",
    ]
    for k in sorted(existing):
        v = existing[k]
        # Quote wanneer nodig
        if any(ch in v for ch in " #"):
            lines.append(f'{k}="{v}"')
        else:
            lines.append(f"{k}={v}")
    secrets_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _chmod_600(secrets_path)
