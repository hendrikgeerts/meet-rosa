"""Decision log — capture beslissingen die later context behoeven.

CEO-werk: vendor-keuze, scope-aanpassing, hire/fire — beslissingen die
maanden later context behoeven ('waarom hadden we ook alweer X gekozen?').

Drie tools:
  log_decision(title, body, attendees?, source_ref?)
  find_decisions(query)         — full-text search
  recent_decisions(days, limit) — chronologische lijst

Briefing / dayclose surfacen ook recent gemaakte beslissingen.
"""
