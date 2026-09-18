"""MCP client abstraction and official-SDK stdio implementation."""

from __future__ import annotations

import asyncio
import subprocess
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from queue import Queue
from threading import Thread
from typing import Any, Protocol

from mcp import Client
from mcp.client.stdio import (
    StdioServerParameters,
    get_default_environment,
    stdio_client,
)

from agent_runtime.mcp.config import McpStdioServerConfig
from agent_runtime.mcp.errors import (
    McpCallTimeoutError,
    McpDisconnectedError,
    McpProtocolError,
    McpStartupError,
)


class McpClient(Protocol):
    def connect(self) -> None: ...
    def list_tools(self) -> list[Any]: ...
    def call_tool(
        self, name: str, arguments: dict[str, Any], *, timeout_seconds: float | None = None
    ) -> Any: ...
    def close(self) -> None: ...


class StdioMcpClient:
    """Keep one official MCP SDK Client alive on a dedicated event-loop thread."""

    def __init__(self, config: McpStdioServerConfig) -> None:
        self._config = config
        self._commands: Queue[tuple[str, Any, float | None, Future[Any]]] = Queue()
        self._ready: Future[bool] = Future()
        self._thread: Thread | None = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        if self._connected:
            return
        if self._thread is not None and self._thread.is_alive():
            raise McpStartupError("The MCP client is already starting.")
        self._ready = Future()
        self._thread = Thread(
            target=self._thread_main,
            name=f"mcp-{self._config.server_id}",
            daemon=True,
        )
        self._thread.start()
        try:
            self._ready.result(timeout=self._config.startup_timeout_seconds)
        except FutureTimeoutError as exc:
            self.close()
            raise McpStartupError("The MCP server did not start before its timeout.") from exc
        except Exception as exc:
            self.close()
            raise McpStartupError("The MCP server could not be started.") from exc

    def list_tools(self) -> list[Any]:
        result = self._request(
            "list_tools", None, self._config.startup_timeout_seconds
        )
        tools = getattr(result, "tools", None)
        if not isinstance(tools, list):
            raise McpProtocolError("The MCP server returned invalid tool discovery data.")
        return tools

    def call_tool(
        self, name: str, arguments: dict[str, Any], *, timeout_seconds: float | None = None
    ) -> Any:
        timeout = timeout_seconds or self._config.call_timeout_seconds
        return self._request("call_tool", (name, arguments), timeout)

    def close(self) -> None:
        thread = self._thread
        if thread is None:
            return
        if thread.is_alive():
            completed: Future[Any] = Future()
            self._commands.put(("close", None, None, completed))
            try:
                completed.result(timeout=self._config.startup_timeout_seconds)
            except Exception:
                pass
            thread.join(timeout=self._config.startup_timeout_seconds)
        self._connected = False
        self._thread = None

    def _request(self, operation: str, payload: Any, timeout: float) -> Any:
        if not self._connected or self._thread is None or not self._thread.is_alive():
            raise McpDisconnectedError("The MCP server connection is unavailable.")
        completed: Future[Any] = Future()
        self._commands.put((operation, payload, timeout, completed))
        try:
            return completed.result(timeout=timeout + 1.0)
        except FutureTimeoutError as exc:
            self._connected = False
            raise McpCallTimeoutError(
                "The MCP call did not complete before its timeout."
            ) from exc

    def _thread_main(self) -> None:
        asyncio.run(self._serve())

    async def _serve(self) -> None:
        parameters = StdioServerParameters(
            command=self._config.command,
            args=list(self._config.args),
            cwd=self._config.cwd,
            env={**get_default_environment(), **self._config.env},
        )
        # Server stderr is deliberately discarded. It may contain credentials,
        # local paths, or protocol diagnostics and must not reach API errors or
        # persistent tool-call events.
        transport = stdio_client(parameters, errlog=subprocess.DEVNULL)
        try:
            async with Client(
                transport,
                read_timeout_seconds=self._config.call_timeout_seconds,
            ) as client:
                active: set[asyncio.Task[None]] = set()
                self._connected = True
                if not self._ready.done():
                    self._ready.set_result(True)
                while True:
                    operation, payload, timeout, completed = await asyncio.to_thread(
                        self._commands.get
                    )
                    if operation == "close":
                        if active:
                            _, pending = await asyncio.wait(
                                active,
                                timeout=self._config.startup_timeout_seconds,
                            )
                            for task in pending:
                                task.cancel()
                            if pending:
                                await asyncio.gather(*pending, return_exceptions=True)
                        if not completed.done():
                            completed.set_result(None)
                        break
                    task = asyncio.create_task(
                        self._perform(client, operation, payload, timeout, completed)
                    )
                    active.add(task)
                    task.add_done_callback(active.discard)
        except BaseException as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)
            self._fail_pending()
        finally:
            self._connected = False

    async def _perform(self, client, operation, payload, timeout, completed) -> None:
        try:
            if operation == "list_tools":
                result = await asyncio.wait_for(client.list_tools(), timeout=timeout)
            elif operation == "call_tool":
                name, arguments = payload
                result = await asyncio.wait_for(
                    client.call_tool(
                        name,
                        arguments,
                        read_timeout_seconds=timeout,
                    ),
                    timeout=timeout,
                )
            else:
                raise McpProtocolError("Unknown MCP client operation.")
        except asyncio.TimeoutError:
            # A timed-out protocol exchange is not reused. The owning manager
            # will reject later calls and close the connection at shutdown.
            self._connected = False
            completed.set_exception(
                McpCallTimeoutError("The MCP call did not complete before its timeout.")
            )
        except asyncio.CancelledError:
            self._connected = False
            if not completed.done():
                completed.set_exception(
                    McpDisconnectedError(
                        "The MCP server connection closed during a request."
                    )
                )
            raise
        except McpProtocolError as exc:
            completed.set_exception(exc)
        except Exception:
            self._connected = False
            completed.set_exception(
                McpDisconnectedError(
                    "The MCP server connection failed during a request."
                )
            )
        else:
            completed.set_result(result)

    def _fail_pending(self) -> None:
        while not self._commands.empty():
            _, _, _, completed = self._commands.get_nowait()
            if not completed.done():
                completed.set_exception(McpDisconnectedError(
                    "The MCP server connection closed."
                ))
