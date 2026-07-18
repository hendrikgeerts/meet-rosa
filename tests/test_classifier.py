"""Unit tests for privacy.classifier — regex + keyword + dictionary rules."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from privacy.classifier import Classifier, load_classifier_from_yaml


@pytest.fixture
def basic_classifier() -> Classifier:
    return Classifier(
        confidential_domains=("juristenkantoor-x.nl", "csirt.example.nl"),
        confidential_keywords=("vertrouwelijk", "salaris", "incident", "datalek"),
        vip_domains=("heineken.com", "klant-a.nl"),
        default_label="internal",
    )


def test_default_is_internal(basic_classifier: Classifier) -> None:
    c = basic_classifier.classify(text="Hoi, kan je morgen even bellen?")
    assert c.label == "internal"
    assert c.reason == "default"


def test_iban_triggers_confidential(basic_classifier: Classifier) -> None:
    c = basic_classifier.classify(text="Boeking op NL91ABNA0417164300 graag voor vrijdag.")
    assert c.label == "confidential"
    assert "iban" in c.matched


def test_keyword_triggers_confidential(basic_classifier: Classifier) -> None:
    c = basic_classifier.classify(text="Dit document is strikt vertrouwelijk en mag niet gedeeld.")
    assert c.label == "confidential"
    assert c.matched == ("vertrouwelijk",)


def test_confidential_domain_in_sender(basic_classifier: Classifier) -> None:
    c = basic_classifier.classify(
        text="Bijlage bijgevoegd.",
        sender="advocaat@juristenkantoor-x.nl",
    )
    assert c.label == "confidential"
    assert c.reason == "confidential_domain"


def test_vip_domain_keeps_internal(basic_classifier: Classifier) -> None:
    c = basic_classifier.classify(
        text="Vraag over offerte-uitbreiding.",
        sender="piet@heineken.com",
    )
    assert c.label == "internal"
    assert c.reason == "vip_domain_match"


def test_anthropic_api_key_pattern(basic_classifier: Classifier) -> None:
    text = "Hier is mijn key: sk-ant-api03-AAAAaaaa1111BBBBbbbb2222CCCCcccc3333"
    c = basic_classifier.classify(text=text)
    assert c.label == "confidential"
    assert "api_key" in c.matched


def test_aws_key_pattern(basic_classifier: Classifier) -> None:
    c = basic_classifier.classify(text="creds AKIAIOSFODNN7EXAMPLE for the bucket")
    assert c.label == "confidential"


def test_keyword_match_case_insensitive(basic_classifier: Classifier) -> None:
    c = basic_classifier.classify(text="Re: SECURITY INCIDENT update")
    assert c.label == "confidential"
    assert c.matched == ("incident",)


def test_default_public_with_vip_boosts_to_internal() -> None:
    cls = Classifier(
        confidential_domains=(),
        confidential_keywords=(),
        vip_domains=("klant.nl",),
        default_label="public",
    )
    no_vip = cls.classify(text="generic text")
    with_vip = cls.classify(text="generic text", sender="contact@klant.nl")
    assert no_vip.label == "public"
    assert with_vip.label == "internal"
    assert with_vip.reason == "vip_domain_boost"


def test_load_from_yaml(tmp_path: Path) -> None:
    conf = tmp_path / "conf.yaml"
    conf.write_text(
        yaml.safe_dump({
            "keywords": ["NDA", "geheim"],
            "domains": {
                "legal": ["jurist.nl"],
                "finance": ["accountant.nl"],
            },
        }),
        encoding="utf-8",
    )
    vip = tmp_path / "vip.yaml"
    vip.write_text(
        yaml.safe_dump({
            "organizations": [
                {"name": "Klant", "domains": ["klant.nl", "klant-be.com"]},
            ],
        }),
        encoding="utf-8",
    )

    cls = load_classifier_from_yaml(confidential_path=conf, vip_path=vip)
    assert cls.classify(text="Onder NDA gedeeld").label == "confidential"
    assert cls.classify(text="hi", sender="x@jurist.nl").label == "confidential"
    assert cls.classify(text="hi", sender="x@accountant.nl").label == "confidential"
    assert cls.classify(text="hi", sender="x@klant.nl").reason == "vip_domain_match"
    assert cls.classify(text="hi", sender="x@klant-be.com").label == "internal"


# --- regression: word-boundary keyword matching ---------------------------

def test_keyword_does_not_match_substring(basic_classifier: Classifier) -> None:
    """Bug die we tegenkwamen: 'nda' in 'vandaag' triggerde valse confidential."""
    c = basic_classifier.classify(text="Mark Jansen kwam vandaag langs voor een demo.")
    assert c.label == "internal"
    assert c.reason == "default"


def test_keyword_still_matches_with_punctuation() -> None:
    cls = Classifier(confidential_keywords=("NDA",), default_label="internal")
    assert cls.classify(text="Stuur de NDA naar mij").label == "confidential"
    assert cls.classify(text="(NDA)").label == "confidential"
    assert cls.classify(text="Onder NDA's gedeeld").label == "confidential"
