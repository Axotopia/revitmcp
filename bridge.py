"""
Revit 2027 MCP Bridge — Production Implementation
Protocol: NDJSON (newline-delimited JSON-RPC 2.0)
Threading: asyncio.Lock protects Revit's STA thread
Discovery: env override -> static prefix -> PowerShell enumeration
"""
import asyncio
import json
import os
import subprocess
import uuid
from typing import Any, Optional
from dotenv import load_dotenv

try:
    import win32file
    import win32pipe
    import pywintypes
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False


class RevitBridgeError(Exception):
    pass


class RevitBridge:
    """Singleton bridge with STA-thread protection."""
    _instance = None
    _lock = asyncio.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        load_dotenv()
        self._pipe_prefix = os.getenv("REVIT_PIPE_PREFIX", r"\\.\pipe\revit-mcp")
        self._initialized = True

    # -------------------------------------------------------------------------
    # Pipe Discovery
    # -------------------------------------------------------------------------
    def _find_pipe(self) -> str:
        """
        Discovery order:
        1. Exact REVIT_PIPE_NAME env var.
        2. Static REVIT_PIPE_PREFIX.
        3. Dynamic enumeration via PowerShell (\\\\.\\pipe\\revit-mcp*).
        """
        if not HAS_WIN32:
            raise RevitBridgeError("pywin32 is required. Install with: pip install pywin32")

        explicit = os.getenv("REVIT_PIPE_NAME")
        if explicit:
            return explicit

        try:
            handle = win32file.CreateFile(
                self._pipe_prefix,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0,
                None,
                win32file.OPEN_EXISTING,
                0,
                None,
            )
            win32file.CloseHandle(handle)
            return self._pipe_prefix
        except pywintypes.error:
            pass

        pipes = self._enumerate_pipes()
        if pipes:
            return pipes[0]

        raise RevitBridgeError(
            f"Could not locate Revit MCP pipe. "
            f"Set REVIT_PIPE_NAME to the exact pipe (e.g., \\\\.\\pipe\\revit-mcp-2027-<guid>) "
            f"or ensure Revit is running with the MCP plugin active."
        )

    @staticmethod
    def _enumerate_pipes() -> list[str]:
        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-Command",
                    "Get-ChildItem '\\\\.\\pipe\\' | Where-Object Name -Like 'revit-mcp*' | Select-Object -ExpandProperty Name",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
                return [rf"\\.\pipe\{name}" for name in names]
        except Exception:
            pass
        return []

    # -------------------------------------------------------------------------
    # Low-level NDJSON I/O
    # -------------------------------------------------------------------------
    def _send_jsonrpc(self, pipe_path: str, method: str, params: Any) -> dict:
        request_id = str(uuid.uuid4())
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": request_id,
        }
        message = json.dumps(payload) + "\n"

        handle = win32file.CreateFile(
            pipe_path,
            win32file.GENERIC_READ | win32file.GENERIC_WRITE,
            0,
            None,
            win32file.OPEN_EXISTING,
            0,
            None,
        )
        try:
            win32file.WriteFile(handle, message.encode("utf-8"))

            buffer = ""
            while True:
                try:
                    _hr, data = win32file.ReadFile(handle, 65536)
                    if not data:
                        break
                    buffer += data.decode("utf-8", errors="replace")

                    for line in buffer.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            parsed = json.loads(line)
                            if isinstance(parsed, dict) and parsed.get("id") == request_id:
                                return parsed
                        except json.JSONDecodeError:
                            continue

                except pywintypes.error:
                    break

            for line in buffer.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                    if isinstance(parsed, dict) and parsed.get("id") == request_id:
                        return parsed
                except json.JSONDecodeError:
                    continue

            raise RevitBridgeError("Failed to receive matching JSON-RPC response from pipe")
        finally:
            win32file.CloseHandle(handle)

    # -------------------------------------------------------------------------
    # Response Unwrap
    # -------------------------------------------------------------------------
    @staticmethod
    def _unwrap(envelope: dict) -> Any:
        if not isinstance(envelope, dict):
            raise RevitBridgeError("Malformed envelope from Revit pipe")
        if envelope.get("error") is not None:
            err = envelope["error"]
            raise RevitBridgeError(
                f"Revit error [{err.get('code', '?')}]: {err.get('message', 'unknown')}"
            )
        return envelope.get("result")

    # -------------------------------------------------------------------------
    # Public Async API
    # -------------------------------------------------------------------------
    async def list_mcp_tools(self) -> Any:
        """MCP protocol: tools/list. Normalizes {'tools': [...]} -> [...]."""
        pipe_path = self._find_pipe()
        async with self._lock:
            envelope = await asyncio.to_thread(
                self._send_jsonrpc, pipe_path, "tools/list", {}
            )
        result = self._unwrap(envelope)
        # MCP spec returns ListToolsResult = {"tools": [Tool, ...]}
        if isinstance(result, dict) and "tools" in result:
            return result["tools"]
        return result

    async def run_mcp_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """MCP protocol: tools/call"""
        pipe_path = self._find_pipe()
        async with self._lock:
            envelope = await asyncio.to_thread(
                self._send_jsonrpc,
                pipe_path,
                "tools/call",
                {"name": name, "arguments": arguments},
            )
        return self._unwrap(envelope)


# ---------------------------------------------------------------------------
# Governed Bridge Factory
# ---------------------------------------------------------------------------

_governor_instance = None


def get_governed_bridge():
    """Return a RequestGovernor wrapping the RevitBridge singleton.

    All callers get the same governed instance — dedup, heartbeat, and
    payload auditing are applied transparently.
    """
    global _governor_instance
    if _governor_instance is None:
        from governor import RequestGovernor  # deferred to avoid circular import
        _governor_instance = RequestGovernor(RevitBridge())
    return _governor_instance
