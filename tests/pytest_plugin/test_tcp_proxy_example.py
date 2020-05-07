import asyncio
import time

import pytest
from async_generator import async_generator, yield_

import aiomisc


class EchoServer(aiomisc.service.TCPServer):
    async def handle_client(
            self, reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter
    ):
        chunk = await reader.read(65534)
        while chunk:
            writer.write(chunk)
            chunk = await reader.read(65534)

        writer.close()
        await writer.wait_closed()


@pytest.fixture()
def server_port(aiomisc_unused_port_factory) -> int:
    return aiomisc_unused_port_factory()


@pytest.fixture()
def services(server_port, localhost):
    return [EchoServer(port=server_port, address=localhost)]


@pytest.fixture()
@async_generator
async def proxy(tcp_proxy, localhost, server_port):
    async with tcp_proxy(localhost, server_port) as proxy:
        await yield_(proxy)


async def test_proxy_client_close(proxy):
    reader, writer = await proxy.create_client()
    payload = b"Hello world"

    writer.write(payload)
    response = await asyncio.wait_for(reader.read(1024), timeout=1)

    assert response == payload

    assert not reader.at_eof()
    await proxy.disconnect_all()

    assert await asyncio.wait_for(reader.read(), timeout=1) == b""
    assert reader.at_eof()


async def test_proxy_client_slow(proxy):
    delay = 0.1
    # Proxy will delay each data chunk
    proxy.set_delay(delay)

    reader, writer = await proxy.create_client()
    payload = b"Hello world"

    delta = -time.monotonic()

    writer.write(payload)
    await asyncio.wait_for(reader.read(1024), timeout=2)

    delta += time.monotonic()

    assert delta >= delay


async def test_proxy_client_with_processor(proxy):
    processed_request = b"Never say hello"

    # Patching protocol functions
    proxy.set_content_processors(
        # Process data from client to server
        lambda _: processed_request,

        # Process data from server to client
        lambda chunk: chunk[::-1],
    )

    reader, writer = await proxy.create_client()
    writer.write(b"nevermind")

    response = await reader.read(16)

    assert response == processed_request[::-1]
