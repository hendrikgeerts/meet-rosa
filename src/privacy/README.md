# privacy

## Doel
De enige route waarlangs data het apparaat verlaat richting een externe LLM.
Combineert classificatie (welk gevoeligheidsniveau?), redactie (welke
placeholders?), routing (lokaal vs. extern), en reconstructie (placeholders
weer terug naar originele waarden ná de externe response).

**Status:** classificatie + redactie + reconstructie + audit-logging staan;
routing/pre-flight-scan/spaCy-NER/Presidio/lokaal-LLM-vangnet komen in
volgende commits. De gateway is vandaag pass-through — wel met audit.

## Modules
- `gateway.py` — `Gateway` class. Het **enige** bestand dat
  `models.claude` mag importeren. Single entry point: `gateway.complete(...)`.
- `classifier.py` — regex + keyword + dictionary classifier. Geeft
  `Classification(label, reason, matched)` met label
  `public` / `internal` / `confidential`.
- `redactor.py` — regex + dictionary redactor. Vervangt PII door stabiele
  placeholders en geeft een mapping terug. Coreference behouden via
  `existing_mapping`.
- `reconstructor.py` — pure string-substitutie, plaatsing-onafhankelijk
  (sorteert placeholders op lengte aflopend).

## Public interface
```python
from privacy.gateway import Gateway
from privacy.classifier import Classifier, load_classifier_from_yaml, Classification
from privacy.redactor import Redactor, load_redactor_from_yaml, Redaction
from privacy.reconstructor import reconstruct
```

## Config-keys
Uit `Settings`:
- `default_sensitivity_label` — default voor classifier (default: `internal`)
- `max_external_payload_bytes` — boven deze drempel verplichte review
  (nog niet afgedwongen — TODO)
- `audit_retention_days` — hoe lang de jsonl-bestanden bewaard worden
  (rotatie nog niet geïmplementeerd — TODO)

Uit yaml-bestanden:
- `config/confidential_domains.yaml` — `domains: { group: [d.nl, ...] }` +
  `keywords: [...]`
- `config/vip_contacts.yaml` — `people:` `organizations:` `projects:`

## Privacy-implicaties
Dit IS de privacy-laag. Belangrijkste regels:
1. `from models.claude import ...` mag alleen vóórkomen in `gateway.py`.
   `grep -rn 'from models.claude' src/` moet exact één regel buiten dit
   bestand vinden (de import in `gateway.py` zelf, geteld dubbel).
2. De `Gateway.complete()` audit-record bevat **geen** prompt-content.
3. Roundtrip-identiteit van redactor + reconstructor is een test-asserted
   eigenschap (`tests/test_redactor.py::test_reconstruct_returns_original`).

## Testscenario's
- `tests/test_classifier.py` — 10 tests: regex-categorieën, keywords,
  domeinen, VIP-boost, yaml-loader.
- `tests/test_redactor.py` — 13 tests: regex per categorie, drempel-filter
  voor bedragen, dictionary-vóór-regex, coreference binnen + tussen calls,
  yaml-loader, redact↔reconstruct roundtrip.
- `tests/test_gateway.py` — 3 tests: forwarded args, audit-record-shape,
  payload-leak-guard.
- `tests/test_audit.py` — 4 tests: JSONL-rotatie, ISO-tijdstempel, geen
  content-keys.
