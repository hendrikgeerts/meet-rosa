"""Per-persoon dossier voor Rosa.

Aggregeert alles wat Rosa over één contact weet:
  - VIP-entry uit config/vip_contacts.yaml (naam, aliases, emails, tier,
    relationship, communication_style)
  - Recente comm_items (mail/slack) met deze persoon
  - Plaud-meetings waar deze persoon participant was
  - Open_loops waar `who` matcht
  - Komende calendar-events met deze persoon als attendee

Resultaat: gestructureerd dict dat Rosa direct kan voorlezen of als
basis voor meeting-prep brief (Fase B) gebruikt.
"""
