"""Concept-mailer voor afspraken plannen.

Stroom:
  1. Comm-intel ingest detecteert scheduling-intent op een binnenkomend
     mail-item (intent=question + scheduling-keywords).
  2. propose_reply() pakt 3 vrije slots, genereert een reply-draft via
     Claude (gateway, internal-label), en persisteert in `pending_proposals`.
  3. Stuurt iMessage naar the user met sender + draft-preview + ID.
  4. the user antwoordt:
       'stuur'   → orchestrator-tool send_proposal(id) → mail uit via
                   mail-router (juiste from-account) + calendar event met
                   Google Meet-link. Markeert proposal 'sent'.
       'wijzig'  → orchestrator past via gewone tools aan (cancel oude,
                   nieuwe maken handmatig).
       'niet'    → cancel_proposal(id) → status 'cancelled'.

Reply-mailbox routing: from-address komt van het comm_item zelf:
  - Gmail-ingest item → reply via Gmail OAuth (DST)
  - IMAP-ingest item → reply via SMTP op die IMAP-account (zelfde adres)
Calendar-event gaat altijd via primary calendar (DST).
"""
