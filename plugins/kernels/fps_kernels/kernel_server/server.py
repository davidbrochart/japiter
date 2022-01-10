import os
import json
import asyncio
import signal
from datetime import datetime
from typing import Iterable, Optional, List, Dict, cast

from fastapi import WebSocket, WebSocketDisconnect  # type: ignore
from starlette.websockets import WebSocketState

from .connect import (
    write_connection_file as _write_connection_file,
    read_connection_file,
    launch_kernel,
    connect_channel,
    cfg_t,
)  # type: ignore
from .message import (
    receive_message,
    send_message,
    send_raw_message,
    create_message,
    to_binary,
    from_binary,
    get_channel_parts,
    get_parent_header,
    get_bin_msg,
)  # type: ignore


kernels: dict = {}


class KernelServer:
    def __init__(
        self,
        kernelspec_path: str = "",
        connection_cfg: Optional[cfg_t] = None,
        connection_file: str = "",
        write_connection_file: bool = True,
        capture_kernel_output: bool = True,
    ) -> None:
        self.capture_kernel_output = capture_kernel_output
        self.kernelspec_path = kernelspec_path
        self.connection_cfg = connection_cfg
        self.connection_file = connection_file
        self.write_connection_file = write_connection_file
        self.channel_tasks: List[asyncio.Task] = []
        self.sessions: Dict[str, WebSocket] = {}
        # blocked messages and allowed messages are mutually exclusive
        self.blocked_messages: List[str] = []
        self.allowed_messages: Optional[
            List[str]
        ] = None  # when None, all messages are allowed
        # when [], no message is allowed
        self.setup_connection_file()

    def setup_connection_file(self):
        if self.write_connection_file:
            self.connection_file_path, self.connection_cfg = _write_connection_file(
                self.connection_file
            )
        elif self.connection_file:
            self.connection_file_path = self.connection_file
            self.connection_cfg = read_connection_file(self.connection_file)
        else:
            if self.connection_cfg is None:
                raise RuntimeError("No connection_cfg")
        self.key = cast(str, self.connection_cfg["key"])

    def block_messages(self, message_types: Iterable[str] = []):
        # if using blocked messages, discard allowed messages
        self.allowed_messages = None
        if isinstance(message_types, str):
            message_types = [message_types]
        self.blocked_messages = list(message_types)

    def allow_messages(self, message_types: Optional[Iterable[str]] = None):
        # if using allowed messages, discard blocked messages
        self.blocked_messages = []
        if message_types is None:
            self.allowed_messages = None
            return
        if isinstance(message_types, str):
            message_types = [message_types]
        self.allowed_messages = list(message_types)

    @property
    def connections(self) -> int:
        return len(self.sessions)

    async def start(self) -> None:
        if not self.kernelspec_path:
            raise RuntimeError(
                "Could not find a kernel, maybe you forgot to install one?"
            )
        self.last_activity = {
            "date": datetime.utcnow().isoformat() + "Z",
            "execution_state": "starting",
        }
        self.kernel_process = await launch_kernel(
            self.kernelspec_path, self.connection_file_path, self.capture_kernel_output
        )
        assert self.connection_cfg is not None
        self.shell_channel = connect_channel("shell", self.connection_cfg)
        self.control_channel = connect_channel("control", self.connection_cfg)
        self.iopub_channel = connect_channel("iopub", self.connection_cfg)
        await self._wait_for_ready()
        self.channel_tasks += [
            asyncio.create_task(self.listen_shell()),
            asyncio.create_task(self.listen_control()),
            asyncio.create_task(self.listen_iopub()),
        ]

    async def stop(self) -> None:
        # FIXME: stop kernel in a better way
        try:
            self.kernel_process.send_signal(signal.SIGINT)
            self.kernel_process.kill()
            await self.kernel_process.wait()
        except Exception:
            pass
        try:
            os.remove(self.connection_file_path)
        except Exception:
            pass
        for task in self.channel_tasks:
            task.cancel()
        self.channel_tasks = []

    async def restart(self) -> None:
        await self.stop()
        self.setup_connection_file()
        await self.start()

    async def serve(self, websocket: WebSocket, session_id: str):
        self.sessions[session_id] = websocket
        await self.listen_web(websocket)
        del self.sessions[session_id]

    async def listen_web(self, websocket: WebSocket):
        try:
            while True:
                msg = await websocket.receive_bytes()
                # FIXME: add back message filtering
                channel, parts = get_channel_parts(msg)
                if channel == "shell":
                    send_raw_message(parts, self.shell_channel, self.key)
                elif channel == "control":
                    send_raw_message(parts, self.control_channel, self.key)
        except WebSocketDisconnect:
            pass

    async def listen_shell(self):
        while True:
            parts = await self.shell_channel.recv_multipart()
            parent_header = get_parent_header(parts)
            session = parent_header["session"]
            if session in self.sessions:
                websocket = self.sessions[session]
                bin_msg = get_bin_msg("shell", parts)
                await websocket.send_bytes(bin_msg)

    async def listen_control(self):
        while True:
            parts = await self.control_channel.recv_multipart()
            parent_header = get_parent_header(parts)
            session = parent_header["session"]
            if session in self.sessions:
                websocket = self.sessions[session]
                bin_msg = get_bin_msg("control", parts)
                await websocket.send_bytes(bin_msg)

    async def listen_iopub(self):
        while True:
            parts = await self.iopub_channel.recv_multipart()
            bin_msg = get_bin_msg("iopub", parts)
            for websocket in self.sessions.values():
                try:
                    await websocket.send_bytes(bin_msg)
                except Exception:
                    pass
            # FIXME: add back last_activity update
            # or replace it with control channel retrieving

    async def _wait_for_ready(self):
        while True:
            msg = create_message("kernel_info_request")
            send_message(msg, self.shell_channel, self.key)
            msg = await receive_message(self.shell_channel, 0.2)
            if msg is not None and msg["msg_type"] == "kernel_info_reply":
                msg = await receive_message(self.iopub_channel, 0.2)
                if msg is None:
                    # IOPub not connected, start over
                    pass
                else:
                    break


async def receive_json_or_bytes(websocket):
    assert websocket.application_state == WebSocketState.CONNECTED
    message = await websocket.receive()
    websocket._raise_on_disconnect(message)
    if "text" in message:
        return json.loads(message["text"])
    msg = from_binary(message["bytes"])
    return msg


async def send_json_or_bytes(websocket, msg):
    bmsg = to_binary(msg)
    if bmsg is None:
        await websocket.send_json(msg)
    else:
        await websocket.send_bytes(bmsg)
