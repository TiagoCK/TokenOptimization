"""Pricing lookup. Every rate comes from a versioned JSON file on disk.

PROJECT_BRIEF.md §3: "Never hardcode prices in check logic and never let the
model infer them from memory. Every dollar figure in the output must trace to a
file the user can inspect and correct."

So this module contains no numbers. It reads them, applies published multipliers,
and -- crucially -- refuses to produce a figure when it does not have a rate.
``rate_for`` returns ``Priced | Unpriced``; there is no code path that quietly
returns zero for an unknown model or an unpriceable service tier. §7: "Bias every
estimate conservative and state assumptions in the output itself."
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Mapping

from .frame import TOKEN_FIELDS, Frame, Segment, TokenBuckets
from . import gaps as gapmod
from .gaps import Gap

#: Tokens are priced per million.
TOKENS_PER_MTOK = Decimal("1000000")

DEFAULT_PRICING_DIR = Path(__file__).resolve().parents[2] / "pricing"


class PricingError(Exception):
    """The pricing file is missing, malformed, or internally inconsistent."""


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Priced:
    """A rate that traces to the pricing file."""

    usd_per_mtok: Decimal
    #: How the rate was arrived at, for the report's audit trail.
    basis: str

    def cost_of(self, tokens: int) -> Decimal:
        return self.usd_per_mtok * Decimal(tokens) / TOKENS_PER_MTOK

    @property
    def is_priced(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class Unpriced:
    """No rate available. Deliberately not a zero."""

    reason: str
    gap: Gap

    def cost_of(self, tokens: int) -> None:
        return None

    @property
    def is_priced(self) -> bool:
        return False


Rate = Priced | Unpriced


@dataclass(frozen=True, slots=True)
class Estimate:
    """The estimated cost of a set of token buckets.

    ``unpriced_tokens`` is reported alongside the total rather than folded into
    it, so a partial estimate can never be mistaken for a complete one.
    """

    usd: Decimal
    priced_tokens: int
    unpriced_tokens: int
    gaps: tuple[Gap, ...] = ()

    @property
    def is_complete(self) -> bool:
        return self.unpriced_tokens == 0

    def __add__(self, other: Estimate) -> Estimate:
        if not isinstance(other, Estimate):
            return NotImplemented
        merged = list(self.gaps)
        known = {(g.code, g.scope) for g in merged}
        for gap in other.gaps:
            if (gap.code, gap.scope) not in known:
                merged.append(gap)
                known.add((gap.code, gap.scope))
        return Estimate(
            usd=self.usd + other.usd,
            priced_tokens=self.priced_tokens + other.priced_tokens,
            unpriced_tokens=self.unpriced_tokens + other.unpriced_tokens,
            gaps=tuple(merged),
        )


# --------------------------------------------------------------------------
# The table
# --------------------------------------------------------------------------


def _dec(value: object, where: str) -> Decimal:
    """Parse a rate as Decimal. Rejects floats outright -- see §7."""
    if isinstance(value, float):
        raise PricingError(
            f"{where}: rates must be JSON strings, not floats "
            f"({value!r} would lose precision)"
        )
    if not isinstance(value, (str, int)):
        raise PricingError(f"{where}: expected a decimal string, got {value!r}")
    try:
        return Decimal(str(value))
    except Exception as exc:  # pragma: no cover - malformed input
        raise PricingError(f"{where}: {value!r} is not a decimal") from exc


class PricingTable:
    """Rates loaded from one pricing JSON file."""

    def __init__(self, data: Mapping[str, object], *, path: Path | None = None):
        self._data = data
        self.path = path
        self.version = str(data.get("pricing_version") or "unknown")
        self.fetched_at = str(data.get("fetched_at") or "unknown")
        self.source_url = str(data.get("source_url") or "")

        self._models: Mapping[str, Mapping] = data.get("models") or {}  # type: ignore[assignment]
        self._aliases: Mapping[str, str] = {
            k: v
            for k, v in (data.get("model_aliases") or {}).items()  # type: ignore[union-attr]
            if not k.startswith("_")
        }
        self._speed_overrides: Mapping[str, Mapping] = data.get("speed_overrides") or {}  # type: ignore[assignment]
        self._tier_multipliers: Mapping[str, Mapping] = (
            data.get("service_tier_multipliers") or {}
        )  # type: ignore[assignment]
        self._geo_multipliers: Mapping[str, Mapping] = (
            data.get("inference_geo_multipliers") or {}
        )  # type: ignore[assignment]
        self._unpriced_tiers: Mapping[str, str] = data.get("unpriced_service_tiers") or {}  # type: ignore[assignment]
        self._server_tools: Mapping[str, Mapping] = data.get("server_tools") or {}  # type: ignore[assignment]

        if not self._models:
            raise PricingError(f"{path or '<memory>'}: no models in pricing file")

        # Display name -> canonical id, so "Claude Opus 5" from a Console CSV
        # resolves the same as "claude-opus-5" from the API.
        self._by_display: dict[str, str] = {}
        for model_id, entry in self._models.items():
            display = entry.get("display_name")
            if display:
                self._by_display[_norm(str(display))] = model_id

    # -- loading ---------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> PricingTable:
        p = Path(path)
        if not p.exists():
            raise PricingError(f"pricing file not found: {p}")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PricingError(f"{p}: invalid JSON: {exc}") from exc
        return cls(data, path=p)

    @classmethod
    def load_default(cls, directory: str | Path | None = None) -> PricingTable:
        """Load the newest pricing file by filename (they sort chronologically)."""
        d = Path(directory) if directory else DEFAULT_PRICING_DIR
        candidates = sorted(d.glob("*.json"))
        if not candidates:
            raise PricingError(f"no pricing files in {d}")
        return cls.load(candidates[-1])

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return f"<PricingTable {self.version} ({len(self._models)} models)>"

    # -- model resolution ------------------------------------------------

    def resolve_model(self, model: str | None) -> str | None:
        """Canonical model id, or None if unrecognised.

        Exact id, then alias table, then display name. Never fuzzy-matched --
        a near-miss that silently prices as the wrong model is worse than
        refusing to price at all.
        """
        if not model:
            return None
        if model in self._models:
            return model
        if model in self._aliases:
            return self._aliases[model]
        return self._by_display.get(_norm(model))

    @property
    def model_ids(self) -> tuple[str, ...]:
        return tuple(self._models)

    def tier_of(self, model: str | None) -> str | None:
        """Frontier / mid / small, for C8's model mix. None if unknown."""
        canonical = self.resolve_model(model)
        if canonical is None:
            return None
        return self._models[canonical].get("tier")

    # -- rates -----------------------------------------------------------

    def rate_for(
        self,
        model: str | None,
        bucket: str,
        *,
        service_tier: str | None = None,
        inference_geo: str | None = None,
        speed: str | None = None,
    ) -> Rate:
        """The per-MTok rate for one token bucket under one set of dimensions."""
        if bucket not in TOKEN_FIELDS:
            raise KeyError(f"unknown token bucket {bucket!r}")

        # A tier we cannot price poisons the whole row, regardless of model.
        if service_tier and service_tier in self._unpriced_tiers:
            reason = str(self._unpriced_tiers[service_tier])
            if service_tier.startswith("priority"):
                gap = gapmod.priority_tier_present(service_tier)
            else:
                gap = gapmod.unpriced_service_tier(service_tier, reason)
            return Unpriced(reason=reason, gap=gap)

        canonical = self.resolve_model(model)
        if canonical is None:
            label = model or "<no model>"
            return Unpriced(
                reason=f"model {label!r} is not in pricing file {self.version}",
                gap=gapmod.unknown_model(label),
            )

        basis_parts: list[str] = []

        # Fast mode replaces the base rates outright rather than scaling them.
        entry = None
        if speed and speed != "standard":
            override = (self._speed_overrides.get(speed) or {}).get(canonical)
            if override:
                entry = override
                basis_parts.append(f"{speed} mode")
        if entry is None:
            entry = self._models[canonical]

        rates = entry.get("rates") or {}
        if bucket not in rates:
            return Unpriced(
                reason=f"no {bucket!r} rate for {canonical!r}",
                gap=gapmod.unknown_model(canonical),
            )

        where = f"{canonical}.rates.{bucket}"
        rate = _dec(rates[bucket].get("usd_per_mtok"), where)
        basis_parts.insert(0, f"{canonical} {bucket} ${rate}/MTok")

        if service_tier:
            multiplier = self._tier_multipliers.get(service_tier)
            if multiplier is None:
                return Unpriced(
                    reason=f"no multiplier for service tier {service_tier!r}",
                    gap=gapmod.unpriced_service_tier(
                        service_tier, "Not present in the pricing file."
                    ),
                )
            factor = _dec(multiplier.get("value"), f"service_tier.{service_tier}")
            if factor != 1:
                rate *= factor
                basis_parts.append(f"x{factor} ({service_tier})")

        if inference_geo:
            multiplier = self._geo_multipliers.get(inference_geo)
            if multiplier is not None:
                factor = _dec(multiplier.get("value"), f"inference_geo.{inference_geo}")
                if factor != 1:
                    rate *= factor
                    basis_parts.append(f"x{factor} (inference_geo={inference_geo})")

        return Priced(usd_per_mtok=rate, basis=" ".join(basis_parts))

    def web_search_rate(self) -> Decimal:
        entry = self._server_tools.get("web_search") or {}
        return _dec(entry.get("usd_per_1000_requests"), "server_tools.web_search")

    # -- estimation ------------------------------------------------------

    def estimate_tokens(self, tokens: TokenBuckets, segment: Segment) -> Estimate:
        """Estimated cost of one segment's token buckets."""
        total = Decimal("0")
        priced = 0
        unpriced = 0
        collected: dict[tuple[str, str], Gap] = {}

        for bucket in TOKEN_FIELDS:
            count = getattr(tokens, bucket)
            if count == 0:
                continue
            rate = self.rate_for(
                segment.model,
                bucket,
                service_tier=segment.service_tier,
                inference_geo=segment.inference_geo,
                speed=segment.speed,
            )
            if rate.is_priced:
                total += rate.cost_of(count)  # type: ignore[union-attr]
                priced += count
            else:
                unpriced += count
                collected[(rate.gap.code, rate.gap.scope)] = rate.gap  # type: ignore[union-attr]

        return Estimate(
            usd=total,
            priced_tokens=priced,
            unpriced_tokens=unpriced,
            gaps=tuple(collected.values()),
        )

    def estimate_frame(self, frame: Frame) -> Estimate:
        """Estimated cost of a whole Frame, including web search requests."""
        estimate = Estimate(Decimal("0"), 0, 0)
        for row in frame.usage:
            estimate = estimate + self.estimate_tokens(row.tokens, row.segment)

        searches = frame.web_search_requests
        if searches:
            estimate = estimate + Estimate(
                usd=self.web_search_rate() * Decimal(searches) / Decimal("1000"),
                priced_tokens=0,
                unpriced_tokens=0,
            )
        return estimate

    # -- provenance ------------------------------------------------------

    def iter_rates(self) -> Iterable[tuple[str, str, str, Decimal, str]]:
        """(group, model_id, bucket, rate, provenance) for every rate on file.

        Used by the provenance test to re-derive every entry marked 'derived'.
        """
        for model_id, entry in self._models.items():
            for bucket, rate in (entry.get("rates") or {}).items():
                yield (
                    "models",
                    model_id,
                    bucket,
                    _dec(rate.get("usd_per_mtok"), f"{model_id}.{bucket}"),
                    str(rate.get("provenance", "")),
                )
        for speed, models in self._speed_overrides.items():
            for model_id, entry in models.items():
                if model_id.startswith("_"):
                    continue
                for bucket, rate in (entry.get("rates") or {}).items():
                    yield (
                        f"speed_overrides.{speed}",
                        model_id,
                        bucket,
                        _dec(rate.get("usd_per_mtok"), f"{model_id}.{bucket}"),
                        str(rate.get("provenance", "")),
                    )

    def multiplier(self, name: str) -> Decimal:
        entry = (self._data.get("multipliers") or {}).get(name)  # type: ignore[union-attr]
        if entry is None:
            raise PricingError(f"no multiplier {name!r} in pricing file")
        return _dec(entry.get("value"), f"multipliers.{name}")

    def base_rate(self, group: str, model_id: str, bucket: str) -> Decimal:
        if group == "models":
            entry = self._models[model_id]
        else:
            speed = group.split(".", 1)[1]
            entry = self._speed_overrides[speed][model_id]
        return _dec(
            entry["rates"][bucket]["usd_per_mtok"], f"{group}.{model_id}.{bucket}"
        )


def _norm(text: str) -> str:
    """Fold case, spacing and separators for display-name matching."""
    return "".join(ch for ch in text.lower() if ch.isalnum())
