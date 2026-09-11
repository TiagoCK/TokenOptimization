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
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Mapping

from .frame import (
    ALL_TOKEN_FIELDS,
    LONG_CONTEXT_WINDOW,
    TOKEN_FIELDS,
    UNSPLIT_CACHE_WRITE,
    Frame,
    Segment,
    TokenBuckets,
)
from . import gaps as gapmod
from .gaps import Gap

#: Tokens are priced per million.
TOKENS_PER_MTOK = Decimal("1000000")

DEFAULT_PRICING_DIR = Path(__file__).resolve().parents[2] / "pricing"

#: Dimensions that change the price of a token. Absent from an export, every
#: row prices at the baseline for each -- see the PRICING_DIMENSION_NOT_GROUPED
#: gap. (``model`` changes it too, but a row without one is Unpriced outright.)
PRICE_DIMENSIONS: tuple[str, ...] = ("service_tier", "inference_geo", "speed")


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
        long_context = data.get("long_context") or {}
        self._long_context_models: frozenset[str] = frozenset(
            long_context.get("standard_rate_models") or ()  # type: ignore[union-attr]
        )

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
        """Load the newest pricing file by filename (they sort chronologically).

        Rates live at the repo root rather than inside the package so they stay
        visible and editable -- §3 requires the user be able to inspect and
        correct them. The cost is that a non-editable ``pip install .`` leaves
        them behind, so say what to do about it rather than just failing.
        """
        d = Path(directory) if directory else DEFAULT_PRICING_DIR
        candidates = sorted(d.glob("*.json"))
        if not candidates:
            raise PricingError(
                f"no pricing files in {d}\n"
                "  Rates are kept in a top-level pricing/ directory so you can "
                "read and correct them.\n"
                "  Run from a checkout (pip install -e .), or point at the file "
                "directly with --pricing."
            )
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
        """Frontier / mid / small, for C8's model mix. None if unknown.

        A classification from the pricing file, not a published fact -- see
        its ``model_tiers`` note. It never affects a price.
        """
        canonical = self.resolve_model(model)
        if canonical is None:
            return None
        return self._models[canonical].get("tier")

    def long_context_confirmed(self, model: str | None) -> bool:
        """Whether the pricing page confirms standard-rate 1M context for this model.

        False for an unknown model as well as for one the page is silent
        about: absence of a confirmation is not a confirmation.
        """
        canonical = self.resolve_model(model)
        return canonical is not None and canonical in self._long_context_models

    @property
    def long_context_models(self) -> frozenset[str]:
        return self._long_context_models

    def service_tier_multiplier(self, tier: str) -> Decimal:
        """The published multiplier for a service tier, e.g. 0.5 for batch."""
        entry = self._tier_multipliers.get(tier)
        if entry is None:
            raise PricingError(f"no service tier multiplier {tier!r} in pricing file")
        return _dec(entry.get("value"), f"service_tier.{tier}")

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
        if bucket == UNSPLIT_CACHE_WRITE:
            # No rate exists for "a cache write of unknown TTL". Price at the
            # 5-minute rate, the cheaper of the two, so the figure is an
            # explicit floor rather than a guess in an unknown direction. The
            # CACHE_TTL_NOT_SPLIT gap says so and quantifies the exposure.
            bucket = "cache_write_5m"
        elif bucket not in TOKEN_FIELDS:
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

    def merge_model_aliases(self, frame: Frame) -> tuple[Frame, list[Gap]]:
        """Rewrite every spelling of a model to its canonical id.

        The cookbook exporter does no normalisation, and its own published
        sample output lists the same model twice -- so identical-looking keys
        that do not collide are a real hazard. Left alone, one model's spend
        splits across two segments and every downstream share, ranking and
        percentage is computed on a fraction of it.

        Merging is a rewrite of the user's data, so it is done only where the
        pricing file can *prove* two strings are the same model, and every
        merge is reported in the caveats naming the spellings involved. A
        spelling the pricing file cannot resolve is left exactly as it came in:
        there is nothing to prove it is a duplicate of anything.

        Returns the rewritten frame and the gaps describing what changed.
        """
        by_canonical: dict[str, set[str]] = {}
        for row in frame.usage:
            raw = row.segment.model
            if not raw:
                continue
            canonical = self.resolve_model(raw)
            if canonical is None:
                continue  # unresolvable: nothing proves it is a duplicate
            by_canonical.setdefault(canonical, set()).add(raw)

        # Only spellings that actually collide are rewritten. A model spelled
        # one consistent way is left as the user wrote it, even if that is a
        # display name rather than the canonical id.
        rewrite: dict[str, str] = {}
        gaps: list[Gap] = []
        for canonical, spellings in sorted(by_canonical.items()):
            if len(spellings) < 2:
                continue
            for spelling in spellings:
                rewrite[spelling] = canonical
            gaps.append(
                gapmod.model_aliases_merged(canonical, tuple(sorted(spellings)))
            )

        if not rewrite:
            return frame, []

        usage = tuple(
            replace(row, segment=replace(row.segment, model=rewrite[row.segment.model]))
            if row.segment.model in rewrite
            else row
            for row in frame.usage
        )
        cost = tuple(
            replace(row, model=rewrite[row.model]) if row.model in rewrite else row
            for row in frame.cost
        )
        return replace(frame, usage=usage, cost=cost), gaps

    def web_search_rate(self) -> Decimal:
        entry = self._server_tools.get("web_search") or {}
        return _dec(entry.get("usd_per_1000_requests"), "server_tools.web_search")

    # -- estimation ------------------------------------------------------

    def _rates(
        self, tokens: TokenBuckets, segment: Segment
    ) -> Iterable[tuple[str, int, Rate]]:
        """(bucket, count, rate) for every non-empty bucket. The one pricing loop."""
        for bucket in ALL_TOKEN_FIELDS:
            count = getattr(tokens, bucket)
            if count == 0:
                continue
            yield bucket, count, self.rate_for(
                segment.model,
                bucket,
                service_tier=segment.service_tier,
                inference_geo=segment.inference_geo,
                speed=segment.speed,
            )

    def estimate_tokens(self, tokens: TokenBuckets, segment: Segment) -> Estimate:
        """Estimated cost of one segment's token buckets."""
        total = Decimal("0")
        priced = 0
        unpriced = 0
        collected: dict[tuple[str, str], Gap] = {}

        for _, count, rate in self._rates(tokens, segment):
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

    def bucket_costs(self, tokens: TokenBuckets, segment: Segment) -> dict[str, Decimal]:
        """Spend per token bucket, for checks that need the composition.

        Unpriced buckets are absent rather than zero, so a caller summing the
        values gets exactly ``estimate_tokens(...).usd`` and nothing invented.
        """
        return {
            bucket: rate.cost_of(count)  # type: ignore[misc]
            for bucket, count, rate in self._rates(tokens, segment)
            if rate.is_priced
        }

    def estimate_frame(self, frame: Frame) -> Estimate:
        """Estimated cost of a whole Frame, including web search requests.

        Also raises the two gaps that are about the estimate as a whole rather
        than any one rate: a rate applied without the pricing page confirming
        it, and a price-changing dimension the data never broke out.
        """
        estimate = Estimate(Decimal("0"), 0, 0)
        unconfirmed: dict[str, int] = {}
        for row in frame.usage:
            row_estimate = self.estimate_tokens(row.tokens, row.segment)
            estimate = estimate + row_estimate
            if (
                row.segment.context_window == LONG_CONTEXT_WINDOW
                and row_estimate.priced_tokens
                and not self.long_context_confirmed(row.segment.model)
            ):
                label = self.resolve_model(row.segment.model) or str(row.segment.model)
                unconfirmed[label] = unconfirmed.get(label, 0) + row_estimate.priced_tokens

        searches = frame.web_search_requests
        if searches:
            estimate = estimate + Estimate(
                usd=self.web_search_rate() * Decimal(searches) / Decimal("1000"),
                priced_tokens=0,
                unpriced_tokens=0,
            )

        extra: list[Gap] = [
            gapmod.long_context_rate_unconfirmed(model, count)
            for model, count in sorted(unconfirmed.items())
        ]
        if frame.usage:
            missing = tuple(
                d for d in PRICE_DIMENSIONS if not frame.has_dimension(d)
            )
            if missing:
                extra.append(gapmod.pricing_dimension_not_grouped(missing))
        if extra:
            estimate = estimate + Estimate(Decimal("0"), 0, 0, gaps=tuple(extra))
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
