import asyncio

import pytest

from aiomisc import entrypoint


@pytest.fixture(scope="module")
def event_loop():
    with entrypoint() as loop:
        asyncio.set_event_loop(loop)
        yield loop


@pytest.fixture(scope="module")
async def sample_fixture(event_loop):
    yield 1


async def test_using_fixture(sample_fixture, event_loop):
    loop_id = id(asyncio.get_event_loop())
    assert sample_fixture == 1
    assert id(event_loop) == loop_id
