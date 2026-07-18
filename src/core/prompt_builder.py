"""Render SYSTEM_PROMPT per-installatie.

MVP-aanpak (M2 wave 1): één centrale render_system_prompt() die de
huidige SYSTEM_PROMPT-template neemt en:

  1. Alle expliciete markers ${user_name} / ${user_signature} substitueert.
  2. Voor terug-compat: alle letterlijke "Hendrik"/"Hendrik's" vervangt
     door de gebruiker's naam, TENZIJ de gebruiker letterlijk 'Hendrik'
     heet (ROSA_DEV=1 setup met config.user.name=Hendrik).
  3. IMAP-account 'hendrikdpm' als voorbeeld → generieke placeholder.

Later (M2 wave 2): SYSTEM_PROMPT wordt uit blocks samengesteld op basis
van settings.extensions_enabled, zodat een klant zonder Todoist/Slack/
Plaud/Sales/OKR ook geen prompt-instructies daarvoor krijgt.

Deze module doet nu geen feature-conditionele filtering — dat komt in
een volgende commit. Wave 1 = tekst-generifiek zonder gedragswijziging.
"""
from __future__ import annotations

from core.config import Settings


def render_system_prompt(template: str, settings: Settings) -> str:
    """Vervang gebruikers-specifieke markers in het SYSTEM_PROMPT-template.

    Voor Hendrik (config.user.name='Hendrik') is de output identiek aan
    de originele string. Voor een nieuwe klant (bv. 'Alex') worden
    alle Hendrik-refs vervangen door 'Alex'.
    """
    if not template:
        return template

    result = template
    name = (settings.user_name or "you").strip() or "you"
    company = (getattr(settings, "user_company", "") or "").strip()

    # 1. Canonieke placeholders (M2 wave 2 zal deze markers toevoegen op
    #    plekken waar back-compat replace niet volstaat).
    result = result.replace("${user_name}", name)
    result = result.replace("${user_signature}", name)
    result = result.replace(
        "${user_company}", company or "the business you run",
    )

    # 2. Backwards-compat: hardcoded "Hendrik" wordt vervangen tenzij de
    #    user LETTERLIJK Hendrik heet. Order matters: 's eerst zodat we
    #    geen "you's" krijgen na een naïeve replace.
    if name != "Hendrik":
        result = result.replace("Hendrik's", f"{name}'s")
        result = result.replace("Hendrik", name)

        # 2b. Bedrijfscontext. Als de user zijn eigen bedrijf heeft
        #     ingevuld, gebruik die; anders drop de specifieke DST/HGE
        #     zin (blijkt in weekly_retro / ceo_letter).
        if company:
            result = result.replace(
                "DST Templates / HGE Ventures", company,
            )
        else:
            result = result.replace(
                "DST Templates / HGE Ventures", "his company",
            )

    # 3. Voorbeeld IMAP-account 'hendrikdpm' → generieke naam die niet
    #    aan Hendrik gebonden is. Wordt door de wizard-generated
    #    imap_accounts.yaml overschreven met werkelijke labels.
    if name != "Hendrik":
        result = result.replace("account='hendrikdpm'", "account='mymail'")

    return result
