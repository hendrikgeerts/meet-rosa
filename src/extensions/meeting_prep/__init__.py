"""Meeting-prep brief — proactieve PA-laag.

X min vóór elk calendar-event met externe deelnemers stuurt Rosa een
iMessage-prep brief: wie zijn de attendees, wat is de recente mail-
historie met hen, suggested talking points, eventuele open loops.

Dedup via `meeting_preps_sent` tabel — per event slechts 1× sturen.
Skip events zonder externe attendees (intern overleg = geen prep nodig).
"""
