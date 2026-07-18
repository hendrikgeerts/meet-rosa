"""Adapters — convert wizard-payloads naar de bestaande config-file
formaten die Rosa's daemon-code verwacht.

Waarom deze laag bestaat:
De wizard schrijft alle setup-data naar `ROSA_HOME/config.yaml` +
`ROSA_HOME/secrets.env`. Maar de bestaande extension-code (uptime
checker, VIP-brief, confidential-classifier) leest historisch aparte
YAML-files uit `config_dir/`. In plaats van elke extensie aan te
passen laten we de wizard-endpoints óók de per-feature YAML schrijven
in de bestaande format.

Bijkomend voordeel: the user's manueel-bewerkte YAMLs blijven werken —
we voegen alleen een schrijf-pad toe, we veranderen niks aan lezen.
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


def normalize_imap_label(label: str) -> str:
    """Normalize IMAP account-label voor gebruik als YAML `name` én
    voor de secrets.env env-var (`IMAP_<NAME>_PASSWORD`). Deze normalizer
    is de single-source zodat server + adapter niet uit-drift'en.

    Sanitize alle non-alphanumeric karakters naar `_` — dan werkt de
    label als env-var naam (POSIX) én als YAML-compatible key. Gevolg:
    label `my.mail` → `my_mail`, `my-work` → `my_work`, `Personal` →
    `personal`.
    """
    import re
    out = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_")
    return out.lower() or "mymail"


def _config_dir_for(rosa_home: Path) -> Path:
    d = rosa_home / "config"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_yaml(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False,
                       default_flow_style=False),
        encoding="utf-8",
    )
    tmp.replace(path)


def write_vip_contacts(rosa_home: Path, contacts: list[str]) -> Path:
    """`vip_contacts.yaml` in het format dat person_brief-loader leest.

    Wizard krijgt vrije-tekst regels (naam OF email). We classificeren:
    lijkt op een email? → email-veld; anders → name-veld.
    """
    path = _config_dir_for(rosa_home) / "vip_contacts.yaml"
    entries: list[dict] = []
    for line in contacts:
        line = line.strip()
        if not line:
            continue
        if "@" in line:
            entries.append({"email": line})
        else:
            entries.append({"name": line})
    _write_yaml(path, {"vips": entries})
    return path


def write_uptime_targets(rosa_home: Path, urls: list[str]) -> Path:
    """`uptime.yaml` in het format dat uptime.checker.load_targets leest:
    `targets: [{name, url, ...}]`."""
    path = _config_dir_for(rosa_home) / "uptime.yaml"
    targets = []
    for url in urls:
        url = url.strip()
        if not url:
            continue
        # `name` uit host, val terug op de hele URL.
        try:
            from urllib.parse import urlparse
            host = urlparse(url).hostname or url
        except Exception:
            host = url
        targets.append({"name": host, "url": url})
    _write_yaml(path, {"targets": targets})
    return path


def write_confidential_domains(rosa_home: Path, domains: list[str]) -> Path:
    """`confidential_domains.yaml` in het format dat de classifier leest:
    `domains: {group: [d.nl,...]}`. We groeperen alles onder 'wizard'
    zodat categorization mogelijk blijft (user kan handmatig opsplitsen)."""
    path = _config_dir_for(rosa_home) / "confidential_domains.yaml"
    domains = [d.strip() for d in domains if d.strip()]
    data = {"domains": {"wizard": domains}}
    _write_yaml(path, data)
    return path


def write_imap_accounts(
    rosa_home: Path, accounts: list[dict],
) -> Path:
    """`imap_accounts.yaml`. Wachtwoorden zitten al in secrets.env, hier
    alleen de connectie-info. Bestaande loader accepteert `password_env`
    als een KEY-naam waaruit os.environ het wachtwoord ophaalt."""
    path = _config_dir_for(rosa_home) / "imap_accounts.yaml"
    out = []
    for acct in accounts:
        out.append({
            "name": normalize_imap_label(acct["label"]),
            "label": acct["label"],
            "host": acct["host"],
            "port": int(acct.get("port", 993)),
            "ssl": True,
            "username": acct["user"],
            "password_env": acct["password_env"],
            "folders": {"inbox": "INBOX", "sent": "Sent"},
            "enabled": True,
            "poll_interval_seconds": 300,
        })
    _write_yaml(path, {"accounts": out})
    return path


def write_news_sources(rosa_home: Path, feeds: list[str]) -> Path:
    """`news_sources.yaml`. Als er nog geen dedicated loader is,
    slaan we hier alvast neer in een nette shape. market_intel/sources.py
    kan hier later naar gaan kijken."""
    path = _config_dir_for(rosa_home) / "news_sources.yaml"
    feeds = [u.strip() for u in feeds if u.strip()]
    _write_yaml(path, {"sources": [{"url": u} for u in feeds]})
    return path
