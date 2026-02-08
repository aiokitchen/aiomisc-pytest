import asyncio

import pytest


class TestSessionScopeAsyncGenFixture:
    @pytest.fixture(scope="session")
    def event_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            yield loop
        finally:
            loop.close()
            asyncio.set_event_loop(None)

    @pytest.fixture(scope="session")
    async def fixture(self):
        yield

    async def test_using_fixture(self, fixture):
        pass

    async def test_not_using_fixture(self):
        pass
