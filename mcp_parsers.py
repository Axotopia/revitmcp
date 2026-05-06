"""
MCP Response Parsing Helpers
=============================
Shared utilities for parsing Autodesk Revit MCP server responses.

The Revit MCP server returns data in several JSON shapes:
  - Shape A: {"outcome": {"elements": [...]}}
  - Shape A2: {"elements": [...]}
  - Shape B: {"results": {"Element Ids": [...]}}
  - Shape C: {"Element Ids": [...]}

These helpers handle all known shapes with a regex fallback for element IDs.
"""

import json
import logging
import re
from typing import Any

logger = logging.getLogger("revitmcp.parsers")


# ---------------------------------------------------------------------------
# Parameter / field extraction
# ---------------------------------------------------------------------------

def pick(params: dict, keys: list) -> Any:
    """Extract the first matching key's value from a Revit parameter dict.

    Handles both raw values and ``{"value": ...}`` wrapper dicts that the
    Autodesk MCP server sometimes returns.
    """
    for k in keys:
        if k in params:
            v = params[k]
            return v.get("value") if isinstance(v, dict) else v
    return None


# ---------------------------------------------------------------------------
# Raw-text extraction from MCP content envelopes
# ---------------------------------------------------------------------------

def get_raw_text(raw_resp: Any) -> str:
    """Pull the inner JSON text string from an MCP ``content`` response.

    The Autodesk MCP server wraps results as::

        {"content": [{"type": "text", "text": "<json string>"}]}

    This function extracts the first ``text`` payload.  If the response is
    not MCP-wrapped, the whole thing is serialized as a fallback.
    """
    if not isinstance(raw_resp, dict):
        return json.dumps(raw_resp, default=str) if raw_resp else ""
    for itm in raw_resp.get("content", []):
        if isinstance(itm, dict) and itm.get("type") == "text":
            return itm.get("text", "")
    # Fallback: serialize the whole thing
    return json.dumps(raw_resp, default=str)


# ---------------------------------------------------------------------------
# Element ID extraction from query_model responses
# ---------------------------------------------------------------------------

def extract_element_ids(raw_resp: Any) -> list[int]:
    """Extract element IDs from a ``query_model`` MCP response.

    Strategy:
      1. Parse JSON and walk all known response shapes
      2. Regex fallback — scan raw text for 6–8 digit numbers
    """
    ids: list[int] = []
    text = get_raw_text(raw_resp)
    if not text:
        return ids

    # --- JSON parsing ---
    try:
        parsed = json.loads(text)
        # Shape A: outcome.elements
        for el in parsed.get("outcome", {}).get("elements", []):
            eid = (el or {}).get("elementId") or (el or {}).get("id")
            if eid is not None:
                ids.append(int(eid))
        # Shape A2: top-level elements
        if not ids:
            for el in parsed.get("elements", []):
                eid = (el or {}).get("elementId") or (el or {}).get("id")
                if eid is not None:
                    ids.append(int(eid))
        # Shape B: results["Element Ids"]
        if not ids:
            r = parsed.get("results", {})
            if isinstance(r, dict):
                ids = [int(x) for x in r.get("Element Ids", []) if x]
            elif isinstance(r, list):
                ids = [int(x) for x in r if x]
        # Shape C: top-level "Element Ids"
        if not ids and "Element Ids" in parsed:
            ids = [int(x) for x in parsed["Element Ids"] if x]
    except Exception:
        pass

    # --- Regex fallback: scan raw text for 6-8 digit numbers ---
    if not ids:
        ids = [int(m) for m in re.findall(r'\b(\d{6,8})\b', text)]

    return ids


# ---------------------------------------------------------------------------
# Element dict extraction from get_element_data responses
# ---------------------------------------------------------------------------

def extract_elements(raw_resp: Any) -> list[dict]:
    """Extract element dicts from a ``get_element_data`` MCP response.

    Tries all known JSON shapes and returns a list of element dicts.
    """
    text = get_raw_text(raw_resp)
    if not text:
        return []

    try:
        parsed = json.loads(text)
        # Shape A: top-level "elements" list
        if "elements" in parsed and isinstance(parsed["elements"], list):
            return parsed["elements"]
        # Shape A2: outcome.elements
        out_e = parsed.get("outcome", {}).get("elements", [])
        if out_e:
            return out_e
        # Shape B: results dict keyed by element ID
        results = parsed.get("results", {})
        if isinstance(results, dict) and "Element Ids" not in results:
            elems = [v for v in results.values() if isinstance(v, dict)]
            if elems:
                return elems
    except Exception:
        pass

    return []


# ---------------------------------------------------------------------------
# Numeric extraction
# ---------------------------------------------------------------------------

def extract_number(val: Any) -> float:
    """Extract a float from a Revit parameter string like ``'18,975.88 sq ft'``."""
    if isinstance(val, (int, float)):
        return float(val)
    val_str = str(val).replace(',', '')
    match = re.search(r'-?\d+\.?\d*', val_str)
    if match:
        return float(match.group())
    raise ValueError(f"Could not extract number from {val}")


def try_parse_float(val: Any, default: float = 0.0) -> float:
    """Safely attempt to parse a float; return *default* on failure."""
    try:
        return extract_number(val)
    except (ValueError, TypeError, AttributeError):
        return default


# ---------------------------------------------------------------------------
# Constant key lists used across audit tools
# ---------------------------------------------------------------------------

AREA_KEYS = [
    "Area", "area",
    "PROPERTY_LINE_AREA", "SITE_PROPERTY_LINE_AREA",
    "ROOM_AREA", "GSA_SPACE_AREA", "NetArea", "GrossArea",
    "AREA",
]

NAME_KEYS = ["Name", "Mark", "Comments", "ELEM_TYPE_PARAM", "Type Name", "Family"]

LEVEL_KEYS = ["Level", "level", "LEVEL_PARAM"]
