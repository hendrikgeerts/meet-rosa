"""Bidirectional sync tussen pa-agent (reminders + actionable open_loops)
en Todoist.

Push: nieuwe lokale items → Todoist task creation; opslag van remote-id
in `todoist_links` zodat we ze later terugvinden.
Pull: elke tick fetcht alle Rosa-project tasks; completed ones markeren
het matchende lokale item als done (reminder.cancelled_at of
open_loop.status='done').

Eén Todoist-project ('Rosa' default) met labels per bron:
  rosa-reminder, rosa-mail, rosa-slack, rosa-plaud, rosa-meeting

Skipped van push (geen actie voor the user nodig):
  outgoing_request, meeting_action_other — die staan in dayclose
  "Wacht op antwoord" en zijn delegate-tracking, geen taak voor the user zelf.
"""
