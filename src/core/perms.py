"""Centrale file-perm helpers.

Elke sensitive file (db, log, audit, yaml met credential-refs) wordt via
deze helpers aangemaakt/aangeraakt zodat nieuwe bestanden altijd 0600
zijn en parent-dirs 0700 (ook op een frisse checkout / nieuwe Mac).
"""
from __future__ import annotations

import os
from pathlib import Path


def secure_dir(path: Path) -> Path:
    """Maak dir aan (parents ok) met mode 0700. Past bestaande mode ook aan."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def secure_file(path: Path) -> Path:
    """Zet 0600 op bestaand bestand. No-op als path niet bestaat."""
    if path.exists():
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return path


def open_secure(path: Path, mode: str = "a", encoding: str | None = "utf-8"):
    """Open een bestand en garandeer 0600 mode.

    Moet 'new-file born safe' zijn: we zetten umask lokaal, openen via
    os.open met expliciete mode, en geven er daarna een normale file
    object voor terug.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND if "a" in mode else os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    prev_umask = os.umask(0o077)
    try:
        fd = os.open(path, flags, 0o600)
    finally:
        os.umask(prev_umask)
    return os.fdopen(fd, mode, encoding=encoding)


def open_secure_bytes(path: Path):
    """Binary variant van `open_secure`: maakt een nieuw bestand aan met
    mode 0600 (born safe — geen race-window tussen write_bytes en chmod
    waar een ander proces het 0644-bestand zou kunnen lezen).

    Caller is verantwoordelijk voor sluiten; gebruik bij voorkeur in een
    `with`-block."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    prev_umask = os.umask(0o077)
    try:
        fd = os.open(path, flags, 0o600)
    finally:
        os.umask(prev_umask)
    return os.fdopen(fd, "wb")
