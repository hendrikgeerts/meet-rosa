# Rosa — Design system

> Style-tokens voor alle Rosa UI (wizard, local dashboard, klant-portal).
> Afgeleid van marketing-site `hge-ventures.com/rosa/`.
>
> **Merkidentiteit in drie woorden:** transparent, minimal, uncompromising.
>
> **Tone:** formeel-direct, geen marketing-fluff, korte zinnen, concrete
> uitspraken. Voorbeeld: "Geen omweg. Geen tweede gateway."

---

## Color palette

```css
:root {
  /* Primary */
  --rosa-navy: #0f172a;         /* headings, primary text */
  --rosa-charcoal: #1e293b;     /* secondary headings */
  --rosa-text: #334155;         /* body text */

  /* Accent (spaarzaam gebruiken — alleen voor CTA + status) */
  --rosa-green: #10b981;        /* primary CTA, success */
  --rosa-green-hover: #059669;
  --rosa-red: #dc2626;          /* alerts, destructive actions */

  /* Neutrals */
  --rosa-cream: #fafaf5;        /* page background */
  --rosa-white: #ffffff;        /* cards */
  --rosa-border: #e2e8f0;       /* subtle borders */
  --rosa-muted: #94a3b8;        /* secondary text, hints */
  --rosa-muted-bg: #f1f5f9;     /* code blocks, subtle backgrounds */
}
```

## Typography

```css
:root {
  --rosa-font-heading: 'Inter', -apple-system, BlinkMacSystemFont, 'SF Pro Display', sans-serif;
  --rosa-font-body: 'Inter', -apple-system, BlinkMacSystemFont, 'SF Pro Text', sans-serif;
  --rosa-font-mono: 'SF Mono', 'JetBrains Mono', 'Menlo', monospace;
}

/* Weights */
--rosa-weight-regular: 400;
--rosa-weight-medium: 500;
--rosa-weight-semibold: 600;
--rosa-weight-bold: 700;

/* Sizes */
--rosa-text-xs: 0.75rem;   /* 12px — captions, hints */
--rosa-text-sm: 0.875rem;  /* 14px — body-small */
--rosa-text-base: 1rem;    /* 16px — body */
--rosa-text-lg: 1.125rem;  /* 18px — lead paragraph */
--rosa-text-xl: 1.5rem;    /* 24px — h3 */
--rosa-text-2xl: 2rem;     /* 32px — h2 */
--rosa-text-3xl: 2.5rem;   /* 40px — h1 */
```

## Spacing

Consistente 8-punt basis:

```css
--rosa-space-1: 0.25rem;   /* 4px */
--rosa-space-2: 0.5rem;    /* 8px */
--rosa-space-3: 0.75rem;   /* 12px */
--rosa-space-4: 1rem;      /* 16px */
--rosa-space-6: 1.5rem;    /* 24px */
--rosa-space-8: 2rem;      /* 32px */
--rosa-space-12: 3rem;     /* 48px */
--rosa-space-16: 4rem;     /* 64px */
```

## Buttons

Flat, minimal, geen dropshadows. Primary = navy op cream óf green voor CTA.

```css
.rosa-btn {
  padding: 0.625rem 1.25rem;
  border-radius: 4px;
  font-weight: 500;
  font-size: 0.9375rem;
  border: 1px solid transparent;
  transition: background 120ms ease, border-color 120ms ease;
  cursor: pointer;
}

.rosa-btn-primary {
  background: var(--rosa-navy);
  color: var(--rosa-cream);
}
.rosa-btn-primary:hover { background: var(--rosa-charcoal); }

.rosa-btn-cta {
  background: var(--rosa-green);
  color: var(--rosa-white);
}
.rosa-btn-cta:hover { background: var(--rosa-green-hover); }

.rosa-btn-secondary {
  background: transparent;
  color: var(--rosa-navy);
  border-color: var(--rosa-border);
}
.rosa-btn-secondary:hover { background: var(--rosa-muted-bg); }

.rosa-btn-danger {
  background: transparent;
  color: var(--rosa-red);
  border-color: var(--rosa-red);
}
.rosa-btn-danger:hover { background: var(--rosa-red); color: white; }
```

## Cards / panels

Flat, dunne border, geen shadow. Border-radius 6px.

```css
.rosa-card {
  background: var(--rosa-white);
  border: 1px solid var(--rosa-border);
  border-radius: 6px;
  padding: var(--rosa-space-6);
}

.rosa-panel {
  background: var(--rosa-muted-bg);
  border-left: 3px solid var(--rosa-navy);
  padding: var(--rosa-space-4) var(--rosa-space-6);
  border-radius: 0 4px 4px 0;
}

.rosa-callout-info {
  background: var(--rosa-muted-bg);
  border-left: 3px solid var(--rosa-green);
  padding: var(--rosa-space-4);
  font-size: var(--rosa-text-sm);
  color: var(--rosa-text);
}

.rosa-callout-warn {
  background: #fef3c7;
  border-left: 3px solid #d97706;
  padding: var(--rosa-space-4);
  font-size: var(--rosa-text-sm);
}
```

## Form fields

```css
.rosa-label {
  display: block;
  font-weight: 500;
  font-size: 0.9375rem;
  color: var(--rosa-navy);
  margin-bottom: var(--rosa-space-2);
}

.rosa-hint {
  display: block;
  font-size: var(--rosa-text-xs);
  color: var(--rosa-muted);
  margin-top: var(--rosa-space-1);
}

.rosa-input,
.rosa-select,
.rosa-textarea {
  width: 100%;
  padding: 0.625rem 0.875rem;
  border: 1px solid var(--rosa-border);
  border-radius: 4px;
  font-size: 0.9375rem;
  font-family: var(--rosa-font-body);
  background: var(--rosa-white);
  transition: border-color 120ms ease;
}

.rosa-input:focus,
.rosa-select:focus,
.rosa-textarea:focus {
  outline: none;
  border-color: var(--rosa-navy);
}

.rosa-input-error {
  border-color: var(--rosa-red);
}
```

## Wizard-specific components

```css
.rosa-wizard-progress {
  display: flex;
  gap: var(--rosa-space-2);
  margin-bottom: var(--rosa-space-8);
}

.rosa-wizard-progress-step {
  flex: 1;
  height: 4px;
  background: var(--rosa-border);
  border-radius: 2px;
}

.rosa-wizard-progress-step-done { background: var(--rosa-green); }
.rosa-wizard-progress-step-active { background: var(--rosa-navy); }
```

## Voice + tone reference

Uit de marketing-site:

- **"Een persoonlijke AI-assistent die je data niet in iemand anders' cloud zet."**
- **"Geen omweg. Geen tweede gateway."**
- **"Niets ertussen. Uitschrijven met één klik."**

Voor wizard-copy:
- Toelichting bij elk veld: **1-2 zinnen, geen jargon**, geen "we vragen dit omdat...".
- Skip-knop labels: "Later regelen" (niet "Skip").
- Bevestigingen: "Klaar." / "Aangesloten." — kort.
- Errors: benoem het probleem + geef de fix. Voorbeeld: "Anthropic-key ongeldig. Kopieer 'em opnieuw uit console.anthropic.com."

## Emoji-gebruik

Spaarzaam, alleen wanneer functioneel:
- 🛡️ privacy / security
- 🏠 lokale opslag
- 📋 configuratie / lijsten
- ✅ succes / afgerond
- ⚠️ waarschuwing (geel-nuance)
- ❌ blokkerende fout
- 🔌 integratie / koppeling
- 🎯 doel / focus

Geen 🎉 🚀 💥 🌟 — botsen met "geen marketing-fluff"-toon.
