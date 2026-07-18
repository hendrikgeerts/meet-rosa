"""Faillissements- en surseance-monitor.

Pollt de RSS-feed van faillissementsdossier.nl elke 30 min, parsed
bedrijfsdetails (naam, KvK, plaats, hoofdactiviteit, status, curator)
en alerteert via iMessage bij matches op:

  1. KvK-watchlist (hoogste prioriteit — the user's concurrenten/klanten/
     leveranciers; voor v1 nog leeg, te beheren via iMessage-tools)
  2. Hoofd-activiteit keyword (best-effort vangnet voor expliciete
     branche-namers)
  3. Bedrijfsnaam keyword (best-effort vangnet voor namen als
     "Pegasus Signage")

Eerlijke beperking gevonden tijdens onderzoek met the user: voor
AV/narrowcasting is layer 1 dominant. Layer 2/3 vangen ~1-3 hits per
jaar; de meeste echte hits komen via KvK-watchlist.
"""
