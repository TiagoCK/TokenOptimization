"""claude-spend-audit: find cost inefficiencies in Anthropic usage data.

Runs entirely on your machine. Nothing is uploaded, and there is no telemetry.
"""

from .frame import CostRow, Frame, Segment, TokenBuckets, UsageRow
from .gaps import Gap, GapCode, Severity
from .pricing import Estimate, Priced, PricingTable, Unpriced

__version__ = "0.1.0"

__all__ = [
    "CostRow",
    "Estimate",
    "Frame",
    "Gap",
    "GapCode",
    "Priced",
    "PricingTable",
    "Segment",
    "Severity",
    "TokenBuckets",
    "Unpriced",
    "UsageRow",
    "__version__",
]
