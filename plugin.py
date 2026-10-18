"""AI usage source for glance: one table entity plus one entity per tool.

Credential files are expected under `ctx.data_dir / "secrets"` (the
operator bind-mounts them there, see README); API keys come from the hub
secret store through `ctx.secrets`. Nothing is read from the environment.
"""

from __future__ import annotations

from glance.plugin import SourcePlugin

from . import providers

ENTITY_FOR = {"CURSOR": "cursor", "GROK": "grok", "OC.GO": "ocgo", "AGY": "agy", "DEVIN": "devin"}


def _pct(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value) -> str:
    return "—" if value is None else f"{float(value):.0f}"


class Usage(SourcePlugin):
    async def start(self) -> None:
        secrets_dir = self.ctx.data_dir / "secrets"
        providers.configure(secrets_dir, self.ctx.data_dir, dict(self.ctx.secrets))
        self.ctx.on_settings_changed(lambda _s: None)
        self.ctx.every(int(self.ctx.settings["refresh_min"]) * 60, self.refresh)

    async def refresh(self) -> None:
        rows = await providers.fetch_all()
        wanted = [str(n).upper() for n in (self.ctx.settings.get("providers") or [])]
        by_name = {str(row.get("name") or "").upper(): row for row in rows}
        ordered = [by_name[n] for n in wanted if n in by_name] or rows
        lines = []
        for row in ordered:
            tag5, tagw = row.get("tag_5h") or "5h", row.get("tag_week") or "W"
            lines.append(f"{row.get('name')} {tag5}{_fmt(row.get('pct_5h'))} {tagw}{_fmt(row.get('pct_week'))}")
            entity = ENTITY_FOR.get(str(row.get("name") or "").upper())
            if entity:
                self.ctx.entities.publish(entity, {
                    "pct_5h": _pct(row.get("pct_5h")), "pct_week": _pct(row.get("pct_week")), "line": lines[-1],
                })
        self.ctx.entities.publish("rows", {
            "rows": [{k: row.get(k) for k in ("name", "pct_5h", "pct_week", "tag_5h", "tag_week", "note")} for row in ordered],
            "lines": lines,
            "names": [row.get("name") for row in ordered],
        })

    def health(self) -> dict:
        secrets_dir = self.ctx.data_dir / "secrets"
        present = [p.name for p in secrets_dir.iterdir()] if secrets_dir.exists() else []
        return {"secrets_dir": str(secrets_dir), "files": present, "keys": sorted(self.ctx.secrets)}


def create(ctx) -> Usage:
    return Usage(ctx)
