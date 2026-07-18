"""Wekelijkse markt-intel digest voor the user (CEO YourCompany + YourHolding).

Twee domeinen:
- digital_signage: nieuws + kansen voor YourCompany
- ai_models: nieuwe modellen, AI-tooling voor YourHolding

Pipeline (mirror van morning_extras/news.py + comm_intel/ingest):
  1. Worker thread fetcht elke 2u alle RSS-feeds (per domain).
  2. Lokale Llama scoort elk nieuw item op relevantie (0-10) +
     opportunity-flag (partnership / product-gap / klant-pain / concurrent-zet).
  3. Items worden in `market_items` opgeslagen (URL-dedup).
  4. Wekelijks (zondag 11:00 NL): Claude synthese over top 15 items van de
     afgelopen week → trending topics + 💡 kansen + per-domein bullets.
"""
