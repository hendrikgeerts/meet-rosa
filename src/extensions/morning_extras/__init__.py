"""Morning-extras: weer + nieuws-snippets voor de ochtendbriefing.

Geen automatische acties; pure data-fetch + lokale samenvatting die in de
bestaande briefing-context wordt opgenomen. Open-Meteo voor weer (gratis,
geen API-key), publieke RSS-feeds voor nieuws, lokale Llama voor ranking
op the user's interesses (geen externe LLM-call voor de selectie zelf —
alleen de finale briefing-tekst gaat door de Claude/gateway-route)."""
