"""Sensitivity classifier — regex + keyword + dictionary, no LLM (yet).

Returns one of three labels per item:
  public        : safe for any external LLM, no redaction needed
  internal      : may go external **after** redaction
  confidential  : never leaves the machine; local model only

Order of checks (PRIVACY_LAYER §2):
  1. Hard regex (IBAN, API keys) → confidential
  2. Confidential keywords list → confidential
  3. Confidential domains list (from yaml) → confidential
  4. VIP-domain match → at least internal (boost)
  5. Otherwise → settings.default_sensitivity_label

The local-LLM tiebreaker for grey-area items is left for a later commit.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

# Conservative regexes — false negatives are acceptable here because the
# keyword list and the redactor's own NER pass catch the rest. False positives
# at the *classifier* level mean wrongly routing innocuous mail to lokaal-only
# (annoying but safe), so we keep these tight.
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
_API_KEY_RES = (
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"),     # Anthropic
    re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),            # OpenAI-ish
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),               # AWS access key
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),     # GitHub PAT
)


@dataclass(frozen=True)
class Classification:
    label: str
    reason: str
    matched: tuple[str, ...] = field(default_factory=tuple)


class Classifier:
    def __init__(
        self,
        *,
        confidential_domains: tuple[str, ...] = (),
        confidential_keywords: tuple[str, ...] = (),
        vip_domains: tuple[str, ...] = (),
        default_label: str = "internal",
    ) -> None:
        self._conf_domains = tuple(d.lower() for d in confidential_domains)
        # Compile word-boundary regexes once. Substring-match (vorige versie)
        # gaf false positives op korte afkortingen — bv. "nda" matchede in
        # "va**nda**ag". Word-boundary lost dat op.
        self._conf_keyword_res = tuple(
            (kw.lower(), re.compile(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", re.IGNORECASE))
            for kw in confidential_keywords if kw
        )
        self._vip_domains = tuple(d.lower() for d in vip_domains)
        self._default = default_label

    @property
    def _conf_keywords(self) -> tuple[str, ...]:
        """Backwards-compat for older tests/inspection that read the raw list."""
        return tuple(kw for kw, _ in self._conf_keyword_res)

    def classify(self, *, text: str, sender: str | None = None) -> Classification:
        sender_l = (sender or "").lower()

        # 1. Hard regex
        if _IBAN_RE.search(text):
            return Classification("confidential", "iban_match", ("iban",))
        for rx in _API_KEY_RES:
            if rx.search(text):
                return Classification("confidential", "api_key_match", ("api_key",))

        # 2. Confidential keywords (word-boundary, case-insensitive)
        for kw, rx in self._conf_keyword_res:
            if rx.search(text):
                return Classification("confidential", "keyword_match", (kw,))

        # 3. Confidential domains (sender)
        for dom in self._conf_domains:
            if dom and dom in sender_l:
                return Classification("confidential", "confidential_domain", (dom,))

        # 4. VIP domain — boost to at least internal
        for dom in self._vip_domains:
            if dom and dom in sender_l:
                if self._default == "public":
                    return Classification("internal", "vip_domain_boost", (dom,))
                return Classification(self._default, "vip_domain_match", (dom,))

        # 5. Default
        return Classification(self._default, "default")


def load_classifier_from_yaml(
    *,
    confidential_path: Path,
    vip_path: Path | None = None,
    default_label: str = "internal",
) -> Classifier:
    """Build a Classifier from the two yaml files under config/."""
    keywords: list[str] = []
    conf_domains: list[str] = []
    if confidential_path.exists():
        cfg = yaml.safe_load(confidential_path.read_text(encoding="utf-8")) or {}
        keywords = list(cfg.get("keywords") or [])
        domain_groups = cfg.get("domains") or {}
        for group in domain_groups.values():
            conf_domains.extend(group or [])

    vip_domains: list[str] = []
    if vip_path and vip_path.exists():
        cfg = yaml.safe_load(vip_path.read_text(encoding="utf-8")) or {}
        for org in cfg.get("organizations") or []:
            vip_domains.extend(org.get("domains") or [])

    return Classifier(
        confidential_domains=tuple(conf_domains),
        confidential_keywords=tuple(keywords),
        vip_domains=tuple(vip_domains),
        default_label=default_label,
    )
