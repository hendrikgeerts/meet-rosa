"""Source-protocol: elke bron (Gmail/IMAP/Slack) implementeert `fetch_new`.

`fetch_new` krijgt de eerder bekende high-water-marks en returnt een
iterable van CommItem instances die nog niet eerder zijn gezien.
De ingest-loop deduplicates additioneel via `comm_items` UNIQUE INDEX,
dus een source mag conservatief zijn (bv. fetch_new mag overlap teruggeven)."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from extensions.comm_intel.schema import CommItem


class CommSource(Protocol):
    @property
    def source(self) -> str: ...

    @property
    def account(self) -> str: ...

    def fetch_new(
        self,
        *,
        last_external_id: str | None,
        since_unix: int,
        limit: int,
    ) -> Iterable[CommItem]: ...
