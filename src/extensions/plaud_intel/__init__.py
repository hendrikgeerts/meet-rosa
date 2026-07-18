"""Plaud-intel: post-meeting analyse van transcripts naar actiepunten.

Pipeline:
  ~/PlaudInbox/*.txt  →  plaud_transcripts (bestaand, integrations.plaud)
                      →  plaud_meetings (deze module: summary/participants/etc)
                      →  open_loops (actiepunten voor the user én anderen)

Lokaal Llama doet de analyse — body's blijven on-device.
"""
