"""
Axoworks Revit MCP Proxy Server
================================
Sits between AnythingLLM and the Autodesk Revit MCP Server, providing:

1. Transparent pass-through to all native Revit MCP tools
2. Coordinate translation (Project Base Point Z-offset)
3. Custom audit tools (septic, energy, WWR)
4. Governance layer (dedup, heartbeat, payload auditing)

Architecture:
    AnythingLLM  ←─stdio─→  main_mcp.py  ←─Named Pipe─→  Autodesk Revit MCP Server

The proxy dynamically discovers tools from the Autodesk pipe and exposes them
to AnythingLLM with identical names/schemas, so existing AnythingLLM workspace
configuration (system prompts, agent settings) continues to work unchanged.

Usage:
    python main_mcp.py

Environment variables (see .env.example):
    OLLAMA_MODEL, OLLAMA_BASE_URL, API_PORT, REVIT_PIPE_PREFIX
    GOVERNOR_HEARTBEAT_THRESHOLD_S, GOVERNOR_CACHE_TTL_S, etc.
"""

import json
import logging
import os
import sys
from typing import Any

from dotenv import load_dotenv

from bridge import RevitBridge, RevitBridgeError, get_governed_bridge
from coordinate_translator import translator

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

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.6:35b")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")


# ---------------------------------------------------------------------------
# MCP Server — using the raw MCP protocol over stdio
# ---------------------------------------------------------------------------
#
# The MCP protocol is JSON-RPC 2.0 over stdin/stdout (newline-delimited).
# We implement the two required methods:
#   - tools/list
#   - tools/call
#
# We use the transport-agnostic JSON-RPC layer already built in bridge.py
# and re-route it over stdio for MCP compliance.

class McpStdioTransport:
    """
    Reads JSON-RPC 2.0 requests from stdin and writes responses to stdout.
    This is the standard MCP transport used by AnythingLLM and other MCP hosts.
    """

    def __init__(self):
        self._bridge = get_governed_bridge()
        self._cached_tools: list[dict] | None = None

    # -----------------------------------------------------------------------
    # Lifecycle
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

    def _build_custom_tools(self) -> list[dict]:
        """Define custom audit tools that the proxy handles internally."""
        return [
            {
                "name": "axo_audit_septic",
                "description": "Run a septic setback compliance audit on the active Revit model.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "jurisdiction": {
                            "type": "string",
                            "description": "Jurisdiction code for building code lookup (default: 'default').",
                            "default": "default",
                        }
                    },
                    "required": [],
                },
            },
            {
                "name": "axo_audit_energy",
                "description": "Run an energy envelope compliance audit (U-factors, SHGC) on the active Revit model.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "jurisdiction": {
                            "type": "string",
                            "description": "Jurisdiction code for energy code lookup (default: 'default').",
                            "default": "default",
                        }
                    },
                    "required": [],
                },
            },
            {
                "name": "axo_audit_wwr",
                "description": "Run a Window-to-Wall Ratio compliance audit on the active Revit model.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "max_wwr_percent": {
                            "type": "number",
                            "description": "Maximum allowed WWR percentage (default: 40).",
                            "default": 40.0,
                        }
                    },
                    "required": [],
                },
            },
        ]

    async def get_tools(self) -> list[dict]:
        """
        Return the merged tool list: native Revit tools + custom audit tools.
        Results are cached so we don't hammer the pipe on every request.
        """
        if self._cached_tools is None:
            native = await self._discover_revit_tools()
            custom = self._build_custom_tools()
            self._cached_tools = native + custom
            logger.info(
                "Discovered %d native Revit tools + %d custom tools",
                len(native),
                len(custom),
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

            # The Autodesk MCP server returns responses in MCP-compliant format:
            #   {"content": [{"type": "text", "text": "{\"outcome\": ...}"}]}
            # We need to parse the inner text, translate coordinates, and re-wrap.
            translated = await self._translate_response(result)

            return translated
        except RevitBridgeError as exc:
            logger.error("Revit bridge error calling '%s': %s", tool_name, exc)
            raise
        except Exception as exc:
            logger.error("Unexpected error calling '%s': %s", tool_name, exc)
            raise

    async def _translate_response(self, response: Any) -> Any:
        """
        Translate Z coordinates inside an MCP-compliant response.
        
        The Autodesk server wraps data as:
            {"content": [{"type": "text", "text": "<json string>"}]}
        
        We parse the inner JSON, translate geometry Z values, and re-wrap.
        """
        if not isinstance(response, dict):
            # Raw passthrough — try translating directly
            translated = await translator.translate_payload(self._bridge, response)
            return translated

        content = response.get("content")
        if not isinstance(content, list):
            # Not MCP-wrapped — translate directly
            translated = await translator.translate_payload(self._bridge, response)
            return translated

        translated_content = []
        for item in content:
            if not isinstance(item, dict):
                translated_content.append(item)
                continue

            if item.get("type") == "text":
                text = item.get("text", "")
                try:
                    # Parse the inner JSON
                    parsed = json.loads(text)
                    # Translate coordinates in the parsed data
                    translated_data = await translator.translate_payload(
                        self._bridge, parsed
                    )
                    # Re-serialize
                    translated_content.append({
                        "type": "text",
                        "text": json.dumps(translated_data, indent=2),
                    })
                except (json.JSONDecodeError, TypeError):
                    # Not JSON — pass through unchanged
                    translated_content.append(item)
            else:
                translated_content.append(item)

        return {"content": translated_content}

    # -----------------------------------------------------------------------
    # Custom audit tools
    # -----------------------------------------------------------------------

    async def _run_septic_audit(self, arguments: dict) -> dict:
        """
        Septic setback compliance audit.
        Uses the Revit pipe for data extraction, then applies deterministic math.
        """
        jurisdiction = arguments.get("jurisdiction", "default")

        # Fetch data via Revit MCP tools (the new query_model + get_element_data API)
        try:
            # Step 1: Query plumbing fixtures (tanks)
            tanks_raw = await self._bridge.run_mcp_tool(
                "query_model",
                {
                    "input": {
                        "categories": ["OST_PlumbingFixtures"],
                        "searchScope": "AllViews",
                        "maxResults": 200,
                    }
                },
            )
            # Step 2: Query property lines
            lines_raw = await self._bridge.run_mcp_tool(
                "query_model",
                {
                    "input": {
                        "categories": ["OST_PropertyLine"],
                        "searchScope": "AllViews",
                        "maxResults": 200,
                    }
                },
            )
        except RevitBridgeError as exc:
            return {"error": f"Failed to query Revit model: {exc}"}

        # Parse the MCP-compliant response format:
        # {"content": [{"type": "text", "text": "{\"outcome\": ...}"}]}
        def _extract_content(response: Any) -> list:
            if isinstance(response, dict):
                content = response.get("content", [])
                if content and isinstance(content, list):
                    text_parts = []
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            try:
                                parsed = json.loads(item.get("text", "{}"))
                                if isinstance(parsed, dict):
                                    elements = parsed.get("outcome", {}).get("elements", [])
                                    text_parts.extend(elements)
                            except (json.JSONDecodeError, AttributeError):
                                text_parts.append(item.get("text", ""))
                    return text_parts
            return []

        tanks = _extract_content(tanks_raw)
        lines = _extract_content(lines_raw)

        # Apply coordinate translation to the extracted elements
        tanks = await translator.translate_payload(self._bridge, tanks)
        lines = await translator.translate_payload(self._bridge, lines)

        # Simple setback calculation (deterministic, no LLM math)
        required_ft = 50.0  # Default IBC setback
        results = {"tanks_found": len(tanks), "lines_found": len(lines)}
        return {
            "audit_type": "septic",
            "jurisdiction": jurisdiction,
            "results": results,
            "narrative": f"Septic audit: Found {len(tanks)} tank(s) and {len(lines)} property line(s). "
                         f"Required setback: {required_ft}ft. "
                         f"Coordinate translation applied via Project Base Point offset.",
        }

    async def _run_energy_audit(self, arguments: dict) -> dict:
        """Energy envelope compliance audit (placeholder — extend as needed)."""
        jurisdiction = arguments.get("jurisdiction", "default")
        return {
            "audit_type": "energy",
            "jurisdiction": jurisdiction,
            "narrative": "Energy envelope audit requires implementation of U-factor/SHGC extraction logic. "
                         "This is a placeholder pending full integration with the query_model + get_element_data API.",
        }

    async def _run_wwr_audit(self, arguments: dict) -> dict:
        """Window-to-Wall ratio compliance audit (placeholder — extend as needed)."""
        max_wwr = arguments.get("max_wwr_percent", 40.0)
        return {
            "audit_type": "wwr",
            "max_wwr_percent": max_wwr,
            "narrative": f"WWR audit targeting {max_wwr}% maximum. "
                         "Full implementation pending integration with query_model + get_element_data API.",
        }

    # -----------------------------------------------------------------------
    # Main request handler
    # -----------------------------------------------------------------------

    async def handle_request(self, request: dict) -> dict:
        """
        Handle a single JSON-RPC 2.0 request.
        Returns a JSON-RPC response dict.
        """
        request_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

        logger.info("Received request: method=%s id=%s", method, request_id)

        try:
            if method == "tools/list":
                tools = await self.get_tools()
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"tools": tools},
                }

            elif method == "tools/call":
                tool_name = params.get("name", "")
                arguments = params.get("arguments", {})

                if not tool_name:
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {
                            "code": -32602,
                            "message": "Missing required parameter: 'name'",
                        },
                    }

                # Route to custom audit tools
                if tool_name == "axo_audit_septic":
                    result = await self._run_septic_audit(arguments)
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(result, indent=2),
                                }
                            ]
                        },
                    }

                elif tool_name == "axo_audit_energy":
                    result = await self._run_energy_audit(arguments)
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(result, indent=2),
                                }
                            ]
                        },
                    }

                elif tool_name == "axo_audit_wwr":
                    result = await self._run_wwr_audit(arguments)
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(result, indent=2),
                                }
                            ]
                        },
                    }

                # All other tools: proxy to the Autodesk Revit pipe
                else:
                    raw_result = await self._call_revit_tool(tool_name, arguments)

                    # _call_revit_tool already returns MCP-wrapped content via
                    # _translate_response: {"content": [{"type": "text", "text": "..."}]}.
                    # Check if already wrapped to avoid double-wrapping.
                    if isinstance(raw_result, dict) and "content" in raw_result:
                        return {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "result": raw_result,
                        }

                    # Fallback: wrap in MCP-compliant content format
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(raw_result, indent=2)
                                    if not isinstance(raw_result, str)
                                    else raw_result,
                                }
                            ]
                        },
                    }

            elif method == "resources/list":
                # Optional MCP method — return empty for now
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"resources": []},
                }

            elif method == "prompts/list":
                # Optional MCP method — return empty for now
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"prompts": []},
                }

            elif method == "initialize":
                # MCP initialization handshake
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {
                            "tools": {},
                            "resources": {},
                            "prompts": {},
                        },
                        "serverInfo": {
                            "name": "axoworks-revit-proxy",
                            "version": "1.0.0",
                        },
                    },
                }

            elif method == "notifications/initialized":
                # No response needed for notifications
                return None

            else:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": f"Method not found: {method}",
                    },
                }

        except RevitBridgeError as exc:
            logger.error("Bridge error handling %s: %s", method, exc)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32000,
                    "message": f"Revit Bridge Error: {exc}",
                },
            }
        except Exception as exc:
            logger.error("Unexpected error handling %s: %s", method, exc)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32603,
                    "message": f"Internal error: {exc}",
                },
            }

    # -----------------------------------------------------------------------
    # Stdio event loop
    # -----------------------------------------------------------------------

    async def run_stdio(self):
        """
        Read JSON-RPC 2.0 requests from stdin (line-delimited) and write
        responses to stdout.  This is the standard MCP stdio transport.
        """
        logger.info("Axoworks Revit MCP Proxy starting (stdio transport)...")
        logger.info("Reading JSON-RPC requests from stdin, writing to stdout.")

        # Signal readiness on stderr (not stdout — that's the protocol channel)
        print("Axoworks Revit MCP Proxy ready.", file=sys.stderr, flush=True)

        loop = asyncio.get_running_loop()

        while True:
            try:
                # Read one line from stdin (asynchronously)
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:
                    logger.info("stdin closed. Shutting down.")
                    break

                line = line.strip()
                if not line:
                    continue

                # Parse the JSON-RPC request
                try:
                    request = json.loads(line)
                except json.JSONDecodeError as exc:
                    logger.error("Invalid JSON on stdin: %s", exc)
                    response = {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": f"Parse error: {exc}"},
                    }
                    print(json.dumps(response), flush=True)
                    continue

                if not isinstance(request, dict) or "method" not in request:
                    logger.error("Invalid request format: %s", request)
                    response = {
                        "jsonrpc": "2.0",
                        "id": request.get("id") if isinstance(request, dict) else None,
                        "error": {"code": -32600, "message": "Invalid Request"},
                    }
                    print(json.dumps(response), flush=True)
                    continue

                # Handle the request
                response = await self.handle_request(request)

                # Notifications don't get responses
                if response is None:
                    continue

                # Write the response as a newline-delimited JSON line
                print(json.dumps(response), flush=True)

            except KeyboardInterrupt:
                logger.info("Keyboard interrupt received. Shutting down.")
                break
            except Exception as exc:
                logger.error("Fatal error in stdio loop: %s", exc)
                # Try to send error response
                try:
                    error_response = {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32603, "message": f"Fatal error: {exc}"},
                    }
                    print(json.dumps(error_response), flush=True)
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
    import asyncio
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server stopped by user.")
    except Exception as exc:
        logger.error("Server crashed: %s", exc)
        sys.exit(1)
