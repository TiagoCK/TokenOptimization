"""Ingest paths.

The CSV and API paths normalize into the same Frame. The Team/Enterprise
spend report has a different shape -- per person, no time buckets -- and loads
into a SpendReport instead; see enterprise.report for why.
"""

from .api_client import (
    ApiClient,
    ApiError,
    AuthError,
    Credential,
    ProtocolError,
    RateLimited,
    envelope,
    is_envelope,
)
from .api_json import load_cost_json, load_usage_json
from .csv_ingest import load_cost_csv, load_usage_csv
from .enterprise_csv import ENTERPRISE_KIND, detect_enterprise, load_enterprise_csv
from .schema import Profile, SchemaError, SchemaMismatch, select_profile

__all__ = [
    "ApiClient",
    "ApiError",
    "AuthError",
    "Credential",
    "ProtocolError",
    "RateLimited",
    "ENTERPRISE_KIND",
    "detect_enterprise",
    "envelope",
    "is_envelope",
    "load_cost_csv",
    "load_cost_json",
    "load_enterprise_csv",
    "load_usage_csv",
    "load_usage_json",
    "Profile",
    "SchemaError",
    "SchemaMismatch",
    "select_profile",
]
