import asyncio
import base64
import json
import time
import traceback
import uuid

import httpx

url = "http://127.0.0.1/vessel-client.php"

REQUEST_INTERVAL_SHORT = 0.1
REQUEST_INTERVAL_LONG = 2


class SocketClosed(Exception):
    pass


async def call(client: httpx.AsyncClient, session_key: str, fn, *args, timeout):
    resp = await client.post(
        url,
        data={
            "session_key": session_key,
            "fn": fn,
            "args": json.dumps(args),
            "timeout": timeout,
        },
        timeout=3,
    )
    data = resp.json()
    print(f"{fn=} {args=} {data=}")
    if data.get("code", None) != 0:
        if "SOCKET_CLOSED" in data["msg"]:
            raise SocketClosed()
        raise RuntimeError(data["msg"])
    return data["resp"]


async def sender(
    state: dict,
    client: httpx.AsyncClient,
    socket_id: int,
    reader: asyncio.StreamReader,
):
    while state["socket_open"]:
        tosend = await reader.read(1024)
        if not tosend:
            state["socket_open"] = False
            return
        try:
            await call(
                client,
                state["session_key"],
                "tcp_socket_write",
                socket_id,
                base64.b64encode(tosend).decode(),
                timeout=1,
            )
        except SocketClosed:
            state["socket_open"] = False
            return
        state["last_communicate_time"] = time.perf_counter()


async def receiver(
    state: dict,
    client: httpx.AsyncClient,
    socket_id: int,
    writer: asyncio.StreamWriter,
):
    while state["socket_open"]:
        try:
            towrite = await call(
                client,
                state["session_key"],
                "tcp_socket_read",
                socket_id,
                1024,
                timeout=1,
            )
        except SocketClosed:
            state["socket_open"] = False
            return
        towrite_bytes = base64.b64decode(towrite)
        if not towrite_bytes:
            await asyncio.sleep(
                REQUEST_INTERVAL_SHORT
                if time.perf_counter() - state["last_communicate_time"] < 3
                else REQUEST_INTERVAL_LONG
            )
            continue
        writer.write(towrite_bytes)
        state["last_communicate_time"] = time.perf_counter()


class TcpServeConnection:
    def __init__(
        self,
        listen_host: str,
        listen_port: int,
        host: str,
        port: int,
    ):
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.host = host
        self.port = port
        self.client = httpx.AsyncClient()
        self.session_key = "_default_"

    async def serve_connection_raw(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        socket_id = None
        try:
            socket_id = await call(
                self.client,
                self.session_key,
                "tcp_socket_connect",
                self.host,
                self.port,
                timeout=1,
            )
        except httpx.HTTPError:
            import traceback

            traceback.print_exc()
            writer.close()
            return
        except Exception:
            writer.close()
            return

        print(f"{socket_id=}")
        state = {
            "socket_open": True,
            "session_key": self.session_key,
            "last_communicate_time": time.perf_counter(),
        }
        try:
            await asyncio.gather(
                sender(state, self.client, socket_id, reader),
                receiver(state, self.client, socket_id, writer),
            )
        finally:
            state["socket_open"] = False
        try:
            socket_id = await call(
                self.client,
                self.session_key,
                "tcp_socket_close",
                socket_id,
                1024,
                timeout=1,
            )
        except Exception:
            writer.close()
            return

    async def serve_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        try:
            await self.serve_connection_raw(reader, writer)
        except Exception:
            traceback.print_exc()

    async def start_server(self):

        session_id = (
            await self.client.get(
                "http://127.0.0.1/vessel.php", params={"action": "check_session_id"}
            )
        ).text.strip()
        print(f"{session_id=}")

        self.session_key = f"_{uuid.uuid4()}"

        async def start_vessel():
            resp = await self.client.get(
                "http://127.0.0.1/vessel.php",
                params={
                    "action": "serve_over_session",
                    "session_key": self.session_key,
                },
                timeout=100,
            )
            print(f"Vessel server resp: {resp.text[:100]=}")

        vessel_task = asyncio.create_task(start_vessel())
        await asyncio.sleep(0.1)
        server = await asyncio.start_server(
            self.serve_connection, self.listen_host, self.listen_port
        )

        task = asyncio.create_task(server.serve_forever())
        return asyncio.gather(task, vessel_task)


async def main():
    server = TcpServeConnection("127.0.0.1", 8082, "127.0.0.1", 80)
    task = await server.start_server()
    await task


if __name__ == "__main__":
    asyncio.run(main())
