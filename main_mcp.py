"""
Axoworks Revit MCP Proxy Server
================================
Sits between AnythingLLM and the Autodesk Revit MCP Server, providing:

1. Transparent pass-through to all native Revit MCP tools
2. Coordinate translation (Project Base Point Z-offset)
3. Custom audit tools (septic, energy, WWR, floor area, lot area, lot coverage, setback)
4. Governance layer (dedup, heartbeat, payload auditing)

Architecture:
    AnythingLLM  ←─stdio─→  main_mcp.py  ←─Named Pipe─→  Autodesk Revit MCP Server

Usage:
    python main_mcp.py

Environment variables (see .env.example):
    REVIT_PIPE_PREFIX, GOVERNOR_HEARTBEAT_THRESHOLD_S, GOVERNOR_CACHE_TTL_S, etc.
"""

import asyncio
import json
import logging
import os
import sys
from typing import Any

from dotenv import load_dotenv

from bridge import RevitBridgeError, get_governed_bridge
from coordinate_translator import translator
from tool_defs import build_custom_tools
from audit_tools import (
    run_septic_audit,
    run_energy_audit,
    run_wwr_audit,
    run_floor_area_audit,
    run_lot_area_audit,
    run_floor_area_query,
    run_lot_coverage_audit,
    run_setback_audit,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stderr,  # MCP uses stdout for protocol — keep logs on stderr
)
logger = logging.getLogger("revitmcp.mcp_server")

load_dotenv()


# ---------------------------------------------------------------------------
# MCP Server — raw JSON-RPC 2.0 over stdio
# ---------------------------------------------------------------------------

def _wrap_result(result: dict, request_id: Any) -> dict:
    """Wrap an audit result dict into a JSON-RPC MCP content response."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}]
        },
    }


class McpStdioTransport:
    """Reads JSON-RPC 2.0 requests from stdin and writes responses to stdout."""

    def __init__(self):
        self._bridge = get_governed_bridge()
        self._cached_tools: list[dict] | None = None

    # -----------------------------------------------------------------------
    # Tool discovery
    # -----------------------------------------------------------------------

    async def _discover_revit_tools(self) -> list[dict]:
        """Fetch tools from the Autodesk Revit MCP pipe via tools/list."""
        try:
            raw = await self._bridge.list_mcp_tools()
            if isinstance(raw, list):
                return raw
            if isinstance(raw, dict) and "tools" in raw:
                return raw["tools"]
            logger.warning("Unexpected tools/list response format: %s", type(raw))
            return []
        except RevitBridgeError as exc:
            logger.error("Failed to discover Revit tools: %s", exc)
            return []
        except Exception as exc:
            logger.error("Unexpected error discovering Revit tools: %s", exc)
            return []

    async def get_tools(self) -> list[dict]:
        """Return merged tool list: native Revit tools + custom audit tools (cached)."""
        if self._cached_tools is None:
            native = await self._discover_revit_tools()
            custom = build_custom_tools()
            self._cached_tools = native + custom
            logger.info(
                "Discovered %d native Revit tools + %d custom tools",
                len(native), len(custom),
            )
        return self._cached_tools

    def invalidate_cache(self):
        """Force re-discovery on the next tools/list call."""
        self._cached_tools = None

    # -----------------------------------------------------------------------
    # Proxy: call a native Revit tool
    # -----------------------------------------------------------------------

    async def _call_revit_tool(self, tool_name: str, arguments: dict) -> Any:
        """Forward a tool call to the Autodesk Revit MCP pipe."""
        try:
            result = await self._bridge.run_mcp_tool(tool_name, arguments)
            return await self._translate_response(result)
        except RevitBridgeError as exc:
            logger.error("Revit bridge error calling '%s': %s", tool_name, exc)
            raise
        except Exception as exc:
            logger.error("Unexpected error calling '%s': %s", tool_name, exc)
            raise

    async def _translate_response(self, response: Any) -> Any:
        """Translate Z coordinates inside an MCP-compliant response.

        The Autodesk server wraps data as:
            {"content": [{"type": "text", "text": "<json string>"}]}
        We parse the inner JSON, translate geometry Z values, and re-wrap.
        """
        if not isinstance(response, dict):
            return await translator.translate_payload(self._bridge, response)

        content = response.get("content")
        if not isinstance(content, list):
            return await translator.translate_payload(self._bridge, response)

        translated_content = []
        for item in content:
            if not isinstance(item, dict):
                translated_content.append(item)
                continue
            if item.get("type") == "text":
                text = item.get("text", "")
                try:
                    parsed = json.loads(text)
                    translated_data = await translator.translate_payload(
                        self._bridge, parsed
                    )
                    translated_content.append({
                        "type": "text",
                        "text": json.dumps(translated_data, indent=2),
                    })
                except (json.JSONDecodeError, TypeError):
                    translated_content.append(item)
            else:
                translated_content.append(item)

        return {"content": translated_content}

    # -----------------------------------------------------------------------
    # Request dispatch
    # -----------------------------------------------------------------------

    # Map custom tool names to their handler coroutines.
    # Each handler signature: async (bridge, arguments) -> dict
    _CUSTOM_TOOL_HANDLERS = {
        "axo_audit_septic":     run_septic_audit,
        "axo_audit_energy":     run_energy_audit,
        "axo_audit_wwr":        run_wwr_audit,
        "axo_audit_floor_area": run_floor_area_audit,
        "axo_audit_lot_area":   run_lot_area_audit,
        "axo_query_floor_area": run_floor_area_query,
        "axo_audit_lot_coverage": run_lot_coverage_audit,
        "axo_audit_setback":    run_setback_audit,
    }

    async def handle_request(self, request: dict) -> dict | None:
        """Handle a single JSON-RPC 2.0 request and return a response dict."""
        request_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})
        logger.info("Received request: method=%s id=%s", method, request_id)

        try:
            # --- tools/list ---
            if method == "tools/list":
                tools = await self.get_tools()
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"tools": tools},
                }

            # --- tools/call ---
            if method == "tools/call":
                tool_name = params.get("name", "")
                arguments = params.get("arguments", {}) or {}

                if not tool_name:
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32602, "message": "Missing required parameter: 'name'"},
                    }

                # Custom audit tools
                handler = self._CUSTOM_TOOL_HANDLERS.get(tool_name)
                if handler is not None:
                    result = await handler(self._bridge, arguments)
                    return _wrap_result(result, request_id)

                # All other tools: proxy to Autodesk Revit pipe
                raw_result = await self._call_revit_tool(tool_name, arguments)
                if isinstance(raw_result, dict) and "content" in raw_result:
                    return {"jsonrpc": "2.0", "id": request_id, "result": raw_result}
                # Fallback wrap
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{
                            "type": "text",
                            "text": json.dumps(raw_result, indent=2)
                                    if not isinstance(raw_result, str)
                                    else raw_result,
                        }]
                    },
                }

            # --- Optional MCP methods ---
            if method == "resources/list":
                return {"jsonrpc": "2.0", "id": request_id, "result": {"resources": []}}

            if method == "prompts/list":
                return {"jsonrpc": "2.0", "id": request_id, "result": {"prompts": []}}

            if method == "initialize":
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                        "serverInfo": {"name": "axoworks-revit-proxy", "version": "1.0.0"},
                    },
                }

            if method == "notifications/initialized":
                return None  # Notifications need no response

            # --- Unknown method ---
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }

        except RevitBridgeError as exc:
            logger.error("Bridge error handling %s: %s", method, exc)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": f"Revit Bridge Error: {exc}"},
            }
        except Exception as exc:
            logger.error("Unexpected error handling %s: %s", method, exc)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": f"Internal error: {exc}"},
            }

    # -----------------------------------------------------------------------
    # Stdio event loop
    # -----------------------------------------------------------------------

    async def run_stdio(self):
        """Read JSON-RPC 2.0 requests from stdin and write responses to stdout."""
        logger.info("Axoworks Revit MCP Proxy starting (stdio transport)...")
        print("Axoworks Revit MCP Proxy ready.", file=sys.stderr, flush=True)

        loop = asyncio.get_running_loop()

        while True:
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:
                    logger.info("stdin closed. Shutting down.")
                    break

                line = line.strip()
                if not line:
                    continue

                try:
                    request = json.loads(line)
                except json.JSONDecodeError as exc:
                    logger.error("Invalid JSON on stdin: %s", exc)
                    print(json.dumps({
                        "jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": f"Parse error: {exc}"},
                    }), flush=True)
                    continue

                if not isinstance(request, dict) or "method" not in request:
                    logger.error("Invalid request format: %s", request)
                    print(json.dumps({
                        "jsonrpc": "2.0",
                        "id": request.get("id") if isinstance(request, dict) else None,
                        "error": {"code": -32600, "message": "Invalid Request"},
                    }), flush=True)
                    continue

                response = await self.handle_request(request)
                if response is None:
                    continue  # Notifications have no response
                print(json.dumps(response), flush=True)

            except KeyboardInterrupt:
                logger.info("Keyboard interrupt received. Shutting down.")
                break
            except Exception as exc:
                logger.error("Fatal error in stdio loop: %s", exc)
                try:
                    print(json.dumps({
                        "jsonrpc": "2.0", "id": None,
                        "error": {"code": -32603, "message": f"Fatal error: {exc}"},
                    }), flush=True)
                except Exception:
                    pass
                break


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

async def main():
    transport = McpStdioTransport()
    await transport.run_stdio()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server stopped by user.")
    except Exception as exc:
        logger.error("Server crashed: %s", exc)
        sys.exit(1)
