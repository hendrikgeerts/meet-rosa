"""Open-loops tracker — gedeelde tabel voor alles wat "nog te doen" is.

Sources:
  - comm_intel: inkomende mail/Slack met intent=question|task waar the user
                op moet reageren. Closed when een matching outgoing-reply
                in dezelfde thread verschijnt.
  - plaud_intel (later): actiepunten uit meeting-transcripts.
  - manual: door the user via iMessage gemarkeerde items.
"""
