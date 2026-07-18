"""Privacy gateway — the only module that imports models.claude.

Single entry point for every external LLM call. The Anthropic SDK lives behind
this barrier so all egress is observable, classifiable, and (today, for
non-tool calls) redactable.

Today's behaviour:
  - tools given        → Claude direct (no redaction yet — tool-use needs
                          per-tool placeholder reconstruction; that's Niveau B,
                          tracked in STATUS.md). Audit label='tool_use'.
  - confidential       → lokaal model (Ollama). Audit event='local_call'.
  - internal/public/   → Claude **with redaction** als er een Redactor is
    default              ingehaakt: redact(system+messages) → preflight scan →
                         Claude → reconstruct(response). Mapping wordt in een
                         enkele complete()-call geaccumuleerd zodat dezelfde
                         entity dezelfde placeholder krijgt over heel de
                         payload (PRIVACY_LAYER §4.4).
  - geen classifier    → pass-through Claude (back-compat met eerdere fase).

Pre-flight failure → fallback naar lokaal model als beschikbaar (privacy is
een constraint, geen feature). Anders raise.

Anywhere outside this file that does `from models.claude import ...` is a bug.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from core.audit import AuditLogger, PayloadAuditLogger
from models.claude import ClaudeClient
from privacy.classifier import Classification, Classifier
from privacy.preflight import PreflightFailure, scan as preflight_scan
from privacy.reconstructor import reconstruct, reconstruct_with_info
from privacy.redactor import Redactor

log = logging.getLogger(__name__)


# Audit P-2: tasks die force_label="public" mogen gebruiken (rauw naar
# Claude zonder redactor). Beperkt tot bewust-publieke content; uitbreiden
# alleen na expliciete privacy-review.
_FORCE_PUBLIC_ALLOWED_TASKS = frozenset({
    "market_intel_score",   # RSS-headlines (publieke nieuws-content)
    "english_practice_eval",  # generic English-grammar judgment
})


# Wordt achteraan elke geredacteerde system-prompt geplakt. Voorkomt dat
# Claude in zijn output zelf namen verzint (zoals "Deborah" voor [PERSON_001])
# in plaats van de placeholder letterlijk te kopiëren — anders mist de
# reconstructor de match en stuurt de agent verzonnen content naar the user.
_PLACEHOLDER_HINT = (
    "\n\n---\nKRITISCH over placeholders. In de tekst hierboven kunnen "
    "tokens als [PERSON_001], [ORG_001], [EMAIL_001], [ADDRESS_001], "
    "[PHONE_001], [URL_001], [IBAN_001], [PROJECT_001], [DATE_001], "
    "[AMOUNT_001] voorkomen. Dit zijn placeholders voor gevoelige gegevens "
    "die door een lokale redactor zijn vervangen.\n\n"
    "REGELS (strikt):\n"
    "1. Gebruik UITSLUITEND placeholders die letterlijk zo in de tekst "
    "hierboven staan. Scroll terug en check als je twijfelt.\n"
    "2. Verzin GEEN placeholders. [PERSON_001] of [URL_001] schrijven "
    "omdat het 'logisch' lijkt is verboden — als de bijbehorende entity "
    "niet in mijn input staat, sla die zin gewoon over of herformuleer "
    "zonder placeholder.\n"
    "3. Kopieer placeholders LETTERLIJK inclusief de vierkante haken. "
    "Niet parafraseren ('person 1'), niet vertalen, niet samenvatten.\n"
    "4. Liever een minder specifieke zin (geen placeholder) dan een "
    "verzonnen placeholder. Een briefing-regel als 'Meet: [URL_001]' "
    "die niet matched verschijnt aan the user letterlijk — slecht voor UX "
    "en onnauwkeurig."
)


# Light wrappers zodat reconstructed responses dezelfde shape hebben als de
# Anthropic Message objecten die callers verwachten (.content[*].type/.text,
# .stop_reason, .usage). We muteren de Anthropic objecten zelf niet.

@dataclass(frozen=True)
class _TextBlock:
    text: str
    type: str = "text"


@dataclass(frozen=True)
class _ReconstructedResponse:
    content: list[Any]
    stop_reason: str | None
    usage: Any | None
    redactions_applied: int = 0


class Gateway:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        audit: AuditLogger,
        classifier: Classifier | None = None,
        redactor: Redactor | None = None,
        local_client: Any | None = None,   # OllamaClient-shaped: .model + .chat()
        payload_audit: PayloadAuditLogger | None = None,
        cost_db_path: Any | None = None,    # Path — waar cost-tracker naar schrijft
        monthly_budget_usd: float = 0.0,    # 0 = uit
    ) -> None:
        self._claude = ClaudeClient(api_key=api_key, model=model)
        self._audit = audit
        self._classifier = classifier
        self._redactor = redactor
        self._local = local_client
        self._payload_audit = payload_audit
        self._cost_db_path = cost_db_path
        self._monthly_budget_usd = monthly_budget_usd

    def _pre_flight_budget_check(self, *, task: str) -> None:
        """Pre-flight budget-check. Wordt aangeroepen VOOR elke Claude-call
        zodat een run-away niet nog één extra call kan doen. Als de user
        precies op de rand zit gaat deze call door en zal de VOLGENDE
        pre-flight blokkeren — dat is bewust (we willen dat de call die
        het budget overschrijdt zichtbaar wordt in cost-history).

        Raise't BudgetExceeded — NIET gevangen door outer try/except omdat
        die alleen de record-write beschermt."""
        if self._cost_db_path is None or self._monthly_budget_usd <= 0:
            return
        from core.cost_tracker import check_budget
        # Laat BudgetExceeded doorpropaganderen naar caller.
        check_budget(self._cost_db_path, self._monthly_budget_usd, task=task)

    def _record_cost(self, *, task: str, model: str, response: Any) -> None:
        """Log cost. Called NA elke Claude-call. Enforce is separate
        (`_pre_flight_budget_check`). Deze functie mag alleen record-
        failures loggen — BudgetExceeded raise'n is de pre-flight's job."""
        if self._cost_db_path is None:
            return
        try:
            from core.cost_tracker import record_call
            usage = getattr(response, "usage", None)
            tok_in = getattr(usage, "input_tokens", 0) if usage else 0
            tok_out = getattr(usage, "output_tokens", 0) if usage else 0
            record_call(
                self._cost_db_path,
                task=task, model=model,
                tokens_in=int(tok_in or 0),
                tokens_out=int(tok_out or 0),
            )
        except Exception:
            log.exception("cost-tracking write failed (continuing)")

    def _log_payload(
        self, *,
        task: str, label: str, backend: str, model: str,
        system_redacted: str, messages_redacted: list[Any],
        tools_offered: list[str],
        response: Any,
        redactions_applied: int,
        classifier_reason: str | None,
    ) -> None:
        if self._payload_audit is None:
            return
        try:
            usage = getattr(response, "usage", None)
            self._payload_audit.log(
                task=task, label=label, model=model, backend=backend,
                system_redacted=system_redacted,
                messages_redacted=messages_redacted,
                tools_offered=tools_offered,
                response_text=_extract_response_text(response),
                redactions_applied=redactions_applied,
                stop_reason=getattr(response, "stop_reason", None),
                input_tokens=getattr(usage, "input_tokens", None) if usage else None,
                output_tokens=getattr(usage, "output_tokens", None) if usage else None,
                classifier_reason=classifier_reason,
            )
        except Exception:
            log.exception("payload-audit write failed (continuing)")

    def complete(
        self,
        *,
        task: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 2048,
        force_label: str | None = None,
    ) -> Any:
        """Generate completion via gateway with classifier-aware routing.

        `force_label` overrides the classifier — useful for callers that
        already know the sensitivity of their content:
          - "public": no classifier, no redactor → raw to Claude. Use only
            for content already publicly available (RSS news, weather, etc.).
          - "internal": skip classifier, still run redactor → Claude. Useful
            when the input is known internal but you trust caller's labelling.
          - "confidential": force local Llama (errors if no local_client).
          - None (default): run classifier and route accordingly.
        """
        # --- tool-use loops gaan via complete_tool_turn() — die is mapping-aware
        # zodat de orchestrator placeholders consistent kan reconstrueren bij
        # tool-uitvoering. Hier valt alleen het nooit-zou-mogen-gebeuren-pad in:
        # iemand roept gateway.complete() rechtstreeks aan met tools. Houd het
        # backward-compat door redactie toe te passen wanneer redactor wired is.
        if tools:
            if self._redactor is not None:
                response, _ = self.complete_tool_turn(
                    task=task, system=system, messages=messages,
                    tools=tools, max_tokens=max_tokens, mapping={},
                )
                return response
            return self._call_claude(
                task=task, system=system, messages=messages,
                tools=tools, max_tokens=max_tokens, label="tool_use",
            )

        # --- forced labels: callers vouchsafe sensitivity, skip classifier ---
        if force_label == "public":
            # Audit P-2 (28/6): whitelist welke tasks force_label='public'
            # MOGEN gebruiken. Voorkomt dat een caller per ongeluk een
            # interne task als 'public' markeert en de redactor skipt.
            if task not in _FORCE_PUBLIC_ALLOWED_TASKS:
                log.warning(
                    "gateway: force_label='public' geweigerd voor task=%s; "
                    "fallback op classifier-pad", task,
                )
                # Niet rauw doorlaten — val terug op de normale flow.
                force_label = None
            else:
                # Geen classifier, geen redactor — direct naar Claude.
                return self._call_claude(
                    task=task, system=system, messages=messages,
                    tools=None, max_tokens=max_tokens, label="public_forced",
                    classification=None,
                )
        if force_label == "confidential":
            if self._local is None:
                raise RuntimeError(
                    "force_label='confidential' but no local_client wired"
                )
            return self._call_local(
                task=task, system=system, messages=messages,
                max_tokens=max_tokens,
                classification=Classification(label="confidential",
                                               reason="forced_by_caller"),
            )
        # force_label="internal" valt door naar redactor-pad.

        cls: Classification | None
        if force_label == "internal":
            cls = Classification(label="internal", reason="forced_by_caller")
        else:
            cls = self._classify_or_none(system=system, messages=messages)
        label = cls.label if cls else "unclassified"

        # --- confidential → lokaal, geen externe call ---
        if cls and cls.label == "confidential":
            if self._local is None:
                log.warning(
                    "confidential payload but no local model configured "
                    "(reason=%s); falling back to Claude. Privacy policy "
                    "violation — wire OllamaClient into Gateway.", cls.reason,
                )
            else:
                return self._call_local(
                    task=task, system=system, messages=messages,
                    max_tokens=max_tokens, classification=cls,
                )

        # --- internal/public/default → redact (if available) → Claude → reconstruct ---
        if self._redactor is not None:
            return self._call_claude_redacted(
                task=task, system=system, messages=messages,
                max_tokens=max_tokens, classification=cls,
            )

        # No redactor wired → legacy pass-through.
        return self._call_claude(
            task=task, system=system, messages=messages,
            tools=None, max_tokens=max_tokens, label=label,
            classification=cls,
        )

    # --- backends -----------------------------------------------------------

    def _call_claude(
        self,
        *,
        task: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
        label: str,
        classification: Classification | None = None,
    ) -> Any:
        # Pre-flight budget check — raise's BudgetExceeded als over cap.
        self._pre_flight_budget_check(task=task)
        response = self._claude.reply(
            system=system, messages=messages,
            tools=tools, max_tokens=max_tokens,
        )
        usage = getattr(response, "usage", None)
        self._audit.log(
            "claude_call",
            task=task, label=label,
            classifier_reason=classification.reason if classification else None,
            model=self._claude.model,
            stop_reason=getattr(response, "stop_reason", None),
            input_tokens=getattr(usage, "input_tokens", None) if usage else None,
            output_tokens=getattr(usage, "output_tokens", None) if usage else None,
            tools_offered=len(tools or []),
            redactions_applied=0,
        )
        self._log_payload(
            task=task, label=label, backend="claude", model=self._claude.model,
            system_redacted=system, messages_redacted=messages,
            tools_offered=[t.get("name", "?") for t in (tools or [])],
            response=response, redactions_applied=0,
            classifier_reason=classification.reason if classification else None,
        )
        self._record_cost(
            task=task, model=self._claude.model, response=response,
        )
        return response

    def complete_tool_turn(
        self,
        *,
        task: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 2048,
        mapping: dict[str, str] | None = None,
    ) -> tuple[Any, dict[str, str]]:
        """Tool-use variant. Returns (response_with_placeholders, accumulated_mapping).

        The orchestrator passes `mapping` through across iterations so the same
        entity gets the same placeholder for the entire conversation turn. The
        orchestrator is responsible for:
          - reconstructing placeholders in tool_use input before executing,
          - feeding the (real) tool_result back as a normal user-message —
            it gets re-redacted on the next call to this method.
        """
        if self._redactor is None:
            response = self._call_claude(
                task=task, system=system, messages=messages,
                tools=tools, max_tokens=max_tokens, label="tool_use",
            )
            return response, mapping or {}

        red_system, red_messages, mapping = self._redact_payload(
            system, messages, existing_mapping=mapping or {},
        )

        try:
            self._preflight(red_system, red_messages)
        except PreflightFailure as exc:
            log.warning(
                "tool-loop pre-flight failed (%s, sample=%r) — aborting turn",
                exc.hit.category, exc.hit.sample,
            )
            self._audit.log(
                "preflight_fallback",
                task=task, label="tool_use",
                category=exc.hit.category,
                redactions_attempted=len(mapping),
            )
            raise

        self._pre_flight_budget_check(task=task)
        response = self._claude.reply(
            system=red_system + _PLACEHOLDER_HINT, messages=red_messages,
            tools=tools, max_tokens=max_tokens,
        )
        usage = getattr(response, "usage", None)
        self._audit.log(
            "claude_call",
            task=task, label="tool_use_redacted",
            classifier_reason=None,
            model=self._claude.model,
            stop_reason=getattr(response, "stop_reason", None),
            input_tokens=getattr(usage, "input_tokens", None) if usage else None,
            output_tokens=getattr(usage, "output_tokens", None) if usage else None,
            tools_offered=len(tools),
            redactions_applied=len(mapping),
        )
        self._log_payload(
            task=task, label="tool_use_redacted", backend="claude", model=self._claude.model,
            system_redacted=red_system, messages_redacted=red_messages,
            tools_offered=[t.get("name", "?") for t in tools],
            response=response, redactions_applied=len(mapping),
            classifier_reason=None,
        )
        self._record_cost(
            task=task, model=self._claude.model, response=response,
        )
        # NB: response.content keeps placeholders (Claude saw them); the
        # orchestrator does the reconstruction on tool inputs and final text.
        return response, mapping

    def _call_claude_redacted(
        self,
        *,
        task: str,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        classification: Classification | None,
    ) -> Any:
        red_system, red_messages, mapping = self._redact_payload(system, messages)

        try:
            self._preflight(red_system, red_messages)
        except PreflightFailure as exc:
            log.warning(
                "pre-flight failed (%s, sample=%r) — rerouting to local model",
                exc.hit.category, exc.hit.sample,
            )
            self._audit.log(
                "preflight_fallback",
                task=task,
                label=classification.label if classification else "unclassified",
                category=exc.hit.category,
                redactions_attempted=len(mapping),
            )
            if self._local is not None:
                return self._call_local(
                    task=task, system=system, messages=messages,
                    max_tokens=max_tokens,
                    classification=classification or _synthetic_class("preflight_fail"),
                )
            raise

        self._pre_flight_budget_check(task=task)
        response = self._claude.reply(
            system=red_system + _PLACEHOLDER_HINT, messages=red_messages,
            tools=None, max_tokens=max_tokens,
        )
        reconstructed, hallucinated = self._reconstruct_response(response, mapping)
        usage = getattr(response, "usage", None)
        label = classification.label if classification else "unclassified"
        self._audit.log(
            "claude_call",
            task=task,
            label=label,
            classifier_reason=classification.reason if classification else None,
            model=self._claude.model,
            stop_reason=getattr(response, "stop_reason", None),
            input_tokens=getattr(usage, "input_tokens", None) if usage else None,
            output_tokens=getattr(usage, "output_tokens", None) if usage else None,
            tools_offered=0,
            redactions_applied=len(mapping),
            hallucinated_placeholders=len(hallucinated),
            hallucinated_categories=sorted(set(hallucinated)),  # leeg = []
        )
        self._log_payload(
            task=task, label=label, backend="claude", model=self._claude.model,
            system_redacted=red_system, messages_redacted=red_messages,
            tools_offered=[], response=response,
            redactions_applied=len(mapping),
            classifier_reason=classification.reason if classification else None,
        )
        self._record_cost(
            task=task, model=self._claude.model, response=response,
        )
        return reconstructed

    def _call_local(
        self,
        *,
        task: str,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        classification: Classification,
    ) -> Any:
        try:
            response = self._local.chat(
                system=system, messages=messages, max_tokens=max_tokens,
            )
        except Exception as exc:
            # M17h: graceful degradation. Ollama kan down zijn tijdens
            # laptop-sleep, na een macOS update, of bij eerste boot vóór
            # user Ollama heeft gestart. Val terug op Claude MET redact
            # — dat is minder ideaal (confidential-content gaat extern)
            # maar beter dan een scheduler-hang die de daemon in
            # SIGTERM-loop stuurt.
            #
            # Fallback vereist een geconfigureerde redactor; anders is er
            # geen safe pad naar Claude en re-raisen we.
            log.warning(
                "local model (%s) failed (%s) for task=%s label=%s",
                getattr(self._local, "model", "?"), exc, task,
                classification.label,
            )
            self._audit.log(
                "local_degraded",
                task=task, label=classification.label,
                classifier_reason=classification.reason,
                model=getattr(self._local, "model", "?"),
                stop_reason=f"exception:{type(exc).__name__}",
                input_tokens=None, output_tokens=None, tools_offered=0,
            )
            if self._redactor is None:
                log.error(
                    "no redactor configured — cannot degrade to Claude "
                    "safely, re-raising",
                )
                raise
            log.warning("degrading to Claude with redact for task=%s", task)
            return self._call_claude_redacted(
                task=task, system=system, messages=messages,
                max_tokens=max_tokens, classification=classification,
            )
        usage = getattr(response, "usage", None)
        self._audit.log(
            "local_call",
            task=task, label=classification.label,
            classifier_reason=classification.reason,
            model=self._local.model,
            stop_reason=getattr(response, "stop_reason", None),
            input_tokens=getattr(usage, "input_tokens", None) if usage else None,
            output_tokens=getattr(usage, "output_tokens", None) if usage else None,
            tools_offered=0,
        )
        # Local calls also written to the shadow log so the user can inspect
        # what went to Ollama (and whether the routing decision was correct).
        self._log_payload(
            task=task, label=classification.label, backend="local",
            model=self._local.model,
            system_redacted=system, messages_redacted=messages,
            tools_offered=[], response=response,
            redactions_applied=0, classifier_reason=classification.reason,
        )
        return response

    # --- redaction helpers --------------------------------------------------

    def _redact_payload(
        self, system: str, messages: list[dict[str, Any]],
        *, existing_mapping: dict[str, str] | None = None,
    ) -> tuple[str, list[dict[str, Any]], dict[str, str]]:
        """Apply redactor consistently across system + every message-content,
        returning the redacted versions plus the cumulative mapping.

        Pass `existing_mapping` to continue numbering across turns of a
        conversation — the same entity keeps the same placeholder."""
        assert self._redactor is not None
        mapping: dict[str, str] = dict(existing_mapping or {})

        def _r(s: str) -> str:
            nonlocal mapping
            if not s:
                return s
            red = self._redactor.redact(s, existing_mapping=mapping)
            mapping = red.mapping
            return red.text

        red_system = _r(system)
        red_messages: list[dict[str, Any]] = []
        for m in messages:
            content = m.get("content")
            if isinstance(content, str):
                red_messages.append({**m, "content": _r(content)})
            elif isinstance(content, list):
                new_blocks: list[Any] = []
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "text":
                        new_blocks.append({**b, "text": _r(str(b.get("text", "")))})
                    elif isinstance(b, dict) and b.get("type") == "tool_result":
                        new_blocks.append({**b, "content": _r(str(b.get("content", "")))})
                    else:
                        new_blocks.append(b)
                red_messages.append({**m, "content": new_blocks})
            else:
                red_messages.append(m)
        return red_system, red_messages, mapping

    def _preflight(self, red_system: str, red_messages: list[dict[str, Any]]) -> None:
        preflight_scan(red_system or "")
        for m in red_messages:
            c = m.get("content")
            if isinstance(c, str):
                preflight_scan(c)
            elif isinstance(c, list):
                for b in c:
                    if isinstance(b, dict):
                        if b.get("type") == "text":
                            preflight_scan(str(b.get("text", "")))
                        elif b.get("type") == "tool_result":
                            preflight_scan(str(b.get("content", "")))

    def _reconstruct_response(
        self, response: Any, mapping: dict[str, str],
    ) -> tuple[_ReconstructedResponse, list[str]]:
        """Reconstruct a Claude response and return (response, hallucinated_categories).

        `hallucinated_categories` lists category-names of placeholders Claude
        invented (not in mapping). Caller logs that count to the audit-stream
        so we can monitor hallucination-rate over time.
        """
        new_blocks: list[Any] = []
        all_leftovers: list[str] = []
        for b in getattr(response, "content", []):
            if getattr(b, "type", None) == "text":
                r = reconstruct_with_info(b.text, mapping, strip_leftover=True)
                new_blocks.append(_TextBlock(text=r.text))
                all_leftovers.extend(r.leftovers)
            else:
                new_blocks.append(b)
        return _ReconstructedResponse(
            content=new_blocks,
            stop_reason=getattr(response, "stop_reason", None),
            usage=getattr(response, "usage", None),
            redactions_applied=len(mapping),
        ), all_leftovers

    # --- classification helper ----------------------------------------------

    def _classify_or_none(
        self, *, system: str, messages: list[dict[str, Any]],
    ) -> Classification | None:
        if self._classifier is None:
            return None
        return self._classifier.classify(text=_extract_text(system, messages))


def _extract_text(system: str, messages: list[dict[str, Any]]) -> str:
    """Verzamel alle plat-tekst uit system + messages voor classificatie."""
    parts: list[str] = []
    if system:
        parts.append(system)
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for b in c:
                if not isinstance(b, dict):
                    continue
                btype = b.get("type")
                if btype == "text":
                    parts.append(str(b.get("text", "")))
                elif btype == "tool_result":
                    parts.append(str(b.get("content", "")))
    return "\n".join(parts)


def _synthetic_class(reason: str) -> Classification:
    return Classification(label="confidential", reason=reason)


def _extract_response_text(response: Any) -> str:
    """Vlakke serialisatie van een response.content lijst — text-blocks
    worden geconcat, tool_use-blocks krijgen een leesbare marker."""
    parts: list[str] = []
    for b in getattr(response, "content", []) or []:
        btype = getattr(b, "type", None)
        if btype == "text":
            parts.append(str(getattr(b, "text", "")))
        elif btype == "tool_use":
            name = getattr(b, "name", "?")
            args = getattr(b, "input", {})
            parts.append(f"[tool_use:{name}({args})]")
        elif btype == "thinking":
            parts.append(f"[thinking:{getattr(b, 'thinking', '')[:80]}…]")
    return "\n".join(parts)
