"""Birthday + jubilea tracker.

Leest extra velden uit `config/vip_contacts.yaml`:
  birthday: "1985-06-12"          # YYYY-MM-DD
  jubilea:                         # lijst van work-anniversaries / partner-data
    - { date: "2018-09-01", label: "Begin DST Templates" }

Tool `upcoming_birthdays(days=14)` voor query, plus briefing-helper
`describe_today_and_upcoming` voor inclusie in dagelijkse briefing.
"""
