"""Travel-alerts: Rosa waarschuwt the user wanneer hij moet vertrekken voor
een agenda-afspraak, op basis van zijn live phone-locatie + actuele
file-info via HERE Maps.

Stroom:
  1. iOS Shortcut op iPhone POST't periodiek (of bij agenda-event-trigger)
     een mail met subject `[PA-LOC]` en body met `lat:` + `lon:` regels.
  2. comm-intel ingest detecteert die subject-prefix → parse coords →
     opslag in `current_location` (laatste positie wint).
  3. Worker thread checkt elke N minuten alle calendar-events met `location`
     in de komende 2 uur. Voor elk:
       - origin = laatst-bekende phone-locatie (cap leeftijd: 2u)
       - HERE Maps: travel-time + traffic-aware duration
       - leave_by = event_start - travel_time - buffer
       - alert via iMessage als (now > leave_by - alert_window) AND niet
         eerder voor dit event gestuurd.
  4. Per (event_id, alert_type) wordt 1× gestuurd via `travel_alerts_sent`
     tabel zodat we niet spammen.
"""
