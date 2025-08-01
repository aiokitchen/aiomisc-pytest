import abc
import asyncio
import concurrent.futures
import logging
import os
import platform
import socket
import sys
import warnings
from asyncio.events import get_event_loop
from contextlib import contextmanager, suppress
from functools import partial, wraps
from inspect import isasyncgenfunction
from types import ModuleType
from typing import (
    Any, AsyncGenerator, Awaitable, Callable, Dict, Generator, Iterable, List,
    Mapping, NamedTuple, Optional, Set, Tuple, Type, Union,
)
from unittest.mock import MagicMock

import aiomisc
import pytest
from aiomisc.compat import set_current_loop, sock_set_reuseport
from aiomisc.utils import TimeoutType, bind_socket
from aiomisc_log import LOG_LEVEL, basic_config


log = logging.getLogger("aiomisc_pytest")


USE_UVLOOP = (
    os.getenv("AIOMISC_USE_UVLOOP", "1").lower() in ("1", "yes", "true")
)

uvloop_module: Optional[ModuleType]

try:
    if USE_UVLOOP:
        import uvloop as _uvloop
        uvloop_module = _uvloop
except ImportError:
    uvloop_module = None


log = logging.getLogger(__name__)
ProxyProcessorType = Callable[[bytes], Awaitable[bytes]]


class Delay:
    __slots__ = "__timeout", "future", "lock"

    def __init__(self) -> None:
        self.__timeout: Union[int, float] = 0
        self.future: Optional[asyncio.Future] = None
        self.lock = asyncio.Lock()

    @property
    def timeout(self) -> Union[int, float]:
        return self.__timeout

    @timeout.setter
    def timeout(self, value: TimeoutType) -> None:
        assert isinstance(value, (int, float))
        assert value >= 0

        self.__timeout = value

        if self.future and not self.future.done():
            self.future.set_result(True)

    async def wait(self) -> None:
        if self.__timeout == 0:
            return

        async with self.lock:
            try:
                self.future = delayed_future(self.__timeout)
                await self.future
            finally:
                self.future = None


def delayed_future(
    timeout: Union[int, float], result: bool = True,
) -> asyncio.Future:

    loop = asyncio.get_event_loop()

    def resolve(f: asyncio.Future) -> None:
        nonlocal result     # noqa

        if f.done():
            return
        f.set_result(result)

    future = loop.create_future()
    handle = loop.call_later(timeout, resolve, future)
    future.add_done_callback(lambda _: handle.cancel())

    return future


class TCPProxy:
    DEFAULT_TIMEOUT = 30

    __slots__ = (
        "proxy_port", "clients", "server", "proxy_host",
        "target_port", "target_host", "listen_host", "read_delay",
        "write_delay", "read_processor", "write_processor", "buffered",
    )

    def __init__(
            self, target_host: str, target_port: int,
            listen_host: str = "127.0.0.1", buffered: bool = True,
    ):
        self.target_port = target_port
        self.target_host = target_host
        self.proxy_host = listen_host
        self.proxy_port = unused_port()
        self.read_delay: TimeoutType = 0
        self.write_delay: TimeoutType = 0
        self.buffered = buffered

        self.clients: Set[TCPProxyClient] = set()
        self.server: Optional[asyncio.AbstractServer] = None

        self.read_processor: Optional[ProxyProcessorType] = None
        self.write_processor: Optional[ProxyProcessorType] = None

    def __repr__(self) -> str:
        return "<{}[{:x}]: tcp://{}:{} => tcp://{}:{}>".format(
            self.__class__.__name__, id(self),
            self.proxy_host, self.proxy_port,
            self.target_host, self.target_port,
        )

    async def start(
        self, timeout: Optional[TimeoutType] = None,
    ) -> asyncio.AbstractServer:
        log.debug("Starting %r", self)
        server = await asyncio.wait_for(
            asyncio.start_server(
                self._handle_client,
                host=self.proxy_host,
                port=self.proxy_port,
            ), timeout=timeout,
        )
        self.server = server
        return server

    ClientType = Tuple[asyncio.StreamReader, asyncio.StreamWriter]

    async def create_client(self) -> ClientType:
        log.debug("Creating client for %r", self)
        return await asyncio.open_connection(
            self.proxy_host, self.proxy_port,
        )

    async def __aenter__(self) -> "TCPProxy":
        if self.server is None:
            await self.start(timeout=self.DEFAULT_TIMEOUT)
        return self

    async def __aexit__(
        self, exc_type: Type[Exception], exc_val: Exception, exc_tb: Any,
    ) -> None:
        await self.close(timeout=self.DEFAULT_TIMEOUT)

    async def close(self, timeout: Optional[TimeoutType] = None) -> None:
        async def close() -> None:
            await self.disconnect_all()

            if self.server is None:
                return

            self.server.close()
            await self.server.wait_closed()

        await asyncio.wait_for(close(), timeout=timeout)

    def set_delay(
        self, read_delay: TimeoutType, write_delay: TimeoutType = 0,
    ) -> None:
        log.debug("Setting delay [R/W %f %f]", read_delay, write_delay)

        for client in self.clients:
            log.debug(
                "Applying delays [R/W: %f %f] for %r",
                read_delay, write_delay, client,
            )
            client.read_delay.timeout = read_delay
            client.write_delay.timeout = write_delay

        self.read_delay = read_delay
        self.write_delay = write_delay

    def set_content_processors(
        self, read: Optional[ProxyProcessorType],
        write: Optional[ProxyProcessorType],
    ) -> None:
        log.debug(
            "Setting content processors for %r: read=%r write=%r",
            self, read, write,
        )

        for client in self.clients:
            log.debug(
                "Applying context processors for %r: read=%r write=%r",
                client, read, write,
            )

            client.read_processor = read
            client.write_processor = write

        self.read_processor = read
        self.write_processor = write

    def disconnect_all(self) -> asyncio.Future:
        log.debug(
            "Disconnecting %s clients of %r", len(self.clients), self,
        )
        return asyncio.ensure_future(
            asyncio.gather(
                *[client.close() for client in self.clients],
                return_exceptions=True,
            ),
        )

    async def _handle_client(
        self, reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        client = TCPProxyClient(reader, writer, buffered=self.buffered)
        self.clients.add(client)

        client.read_delay.timeout = self.read_delay
        client.write_delay.timeout = self.write_delay
        client.read_processor = self.read_processor
        client.write_processor = self.write_processor

        await client.connect(self.target_host, self.target_port)

        client.closing.add_done_callback(
            lambda _: self.clients.remove(client),
        )

    @contextmanager
    def slowdown(
        self, read_delay: TimeoutType = 0, write_delay: TimeoutType = 0,
    ) -> Generator[None, None, None]:
        old_read_delay = self.read_delay
        old_write_delay = self.write_delay

        self.set_delay(read_delay, write_delay)

        try:
            yield
        finally:
            self.set_delay(old_read_delay, old_write_delay)


class TCPProxyClient:
    __slots__ = (
        "client_reader", "client_writer",
        "server_reader", "server_writer",
        "chunk_size", "tasks", "loop",
        "read_delay", "write_delay", "closing",
        "__processors", "__client_repr", "__server_repr",
        "buffered",
    )

    @staticmethod
    async def _blank_processor(body: bytes) -> bytes:
        return body

    def __init__(
        self, client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        chunk_size: int = 64 * 1024, buffered: bool = False,
    ):

        self.loop = asyncio.get_event_loop()
        self.client_reader: asyncio.StreamReader = client_reader
        self.client_writer: asyncio.StreamWriter = client_writer

        self.server_reader: Optional[asyncio.StreamReader] = None
        self.server_writer: Optional[asyncio.StreamWriter] = None

        self.tasks: Iterable[asyncio.Task] = ()
        self.chunk_size = chunk_size  # type: int
        self.read_delay = Delay()
        self.write_delay = Delay()

        self.closing: asyncio.Future = self.loop.create_future()
        self.__processors: Dict[str, ProxyProcessorType] = {
            "read": self._blank_processor, "write": self._blank_processor,
        }

        self.buffered = bool(buffered)

        self.__client_repr = ""
        self.__server_repr = ""

    @property
    def read_processor(self) -> ProxyProcessorType:
        return self.__processors["read"]

    @read_processor.setter
    def read_processor(self, value: Optional[ProxyProcessorType]) -> None:
        if value is None:
            self.__processors["read"] = self._blank_processor
            return
        self.__processors["read"] = aiomisc.awaitable(value)

    @property
    def write_processor(self) -> ProxyProcessorType:
        return self.__processors["write"]

    @write_processor.setter
    def write_processor(self, value: Optional[ProxyProcessorType]) -> None:
        if value is None:
            self.__processors["write"] = self._blank_processor
            return
        self.__processors["write"] = aiomisc.awaitable(value)

    def __repr__(self) -> str:
        return "<{}[{:x}]: {} => {}>".format(
            self.__class__.__name__, id(self),
            self.__client_repr, self.__server_repr,
        )

    @staticmethod
    async def _close_writer(writer: asyncio.StreamWriter) -> None:
        writer.close()
        await writer.wait_closed()

    async def pipe(
        self, reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        processor: str,
        delay: Delay,
    ) -> None:
        try:
            while not reader.at_eof():
                chunk = await reader.read(self.chunk_size)

                if not chunk:
                    break

                if delay.timeout > 0:
                    log.debug(
                        "%r sleeping %.3f seconds on %s",
                        self, delay.timeout, processor,
                    )

                    await delay.wait()

                writer.write(await self.__processors[processor](chunk))

                if not self.buffered:
                    await writer.drain()
        finally:
            await self._close_writer(writer)

    async def connect(self, target_host: str, target_port: int) -> None:
        log.debug("Establishing connection for %r", self)

        self.server_reader, self.server_writer = await asyncio.open_connection(
            host=target_host, port=target_port,
        )

        self.__client_repr = ":".join(
            map(str, self.client_writer.get_extra_info("peername")[:2]),
        )
        self.__server_repr = ":".join(
            map(str, self.server_writer.get_extra_info("peername")[:2]),
        )

        self.tasks = (
            self.loop.create_task(
                self.pipe(
                    self.server_reader,
                    self.client_writer,
                    "write",
                    self.write_delay,
                ),
            ),
            self.loop.create_task(
                self.pipe(
                    self.client_reader,
                    self.server_writer,
                    "read",
                    self.read_delay,
                ),
            ),
        )

    async def close(self) -> None:
        log.debug("Closing %r", self)
        if self.closing.done():
            return

        await aiomisc.cancel_tasks(self.tasks)
        self.loop.call_soon(self.closing.set_result, True)
        await self.closing


def unused_port(*args: Any) -> int:
    with socket.socket(*args) as sock:
        sock.bind(("", 0))
        port = sock.getsockname()[1]
    return port


@contextmanager
def mock_get_event_loop() -> Generator[Any, MagicMock, None]:
    loop_getter = get_event_loop
    getter_mock = MagicMock(asyncio.get_event_loop)
    getter_mock.side_effect = loop_getter

    try:
        asyncio.get_event_loop = getter_mock
        yield getter_mock
    finally:
        asyncio.get_event_loop = get_event_loop


@pytest.fixture(scope="session")
def tcp_proxy() -> Type[TCPProxy]:
    return TCPProxy


def isasyncgenerator(func: Callable[..., Any]) -> Optional[bool]:
    if isasyncgenfunction(func):
        return True
    elif asyncio.iscoroutinefunction(func):
        return False
    return None


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "forbid_get_event_loop: "
        "fail when asyncio.get_event_loop will be called",
    )
    config.addinivalue_line(
        "markers",
        "catch_loop_exceptions: "
        "fails when unhandled loop exception "
        "will be raised",
    )


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("aiomisc plugin options")

    group.addoption(
        "--aiomisc-debug", action="store_true", default=False,
        help="Set debug for event loop",
    )

    group.addoption(
        "--aiomisc-pool-size", type=int, default=4,
        help="Default thread pool size",
    )

    group.addoption(
        "--aiomisc-test-timeout", type=float, default=None,
        help="Test timeout",
    )


def pytest_fixture_setup(fixturedef, request):  # type: ignore
    func = fixturedef.func

    is_async_gen = isasyncgenerator(func)

    if is_async_gen is None:
        return

    strip_request = False
    if "request" not in fixturedef.argnames:
        fixturedef.argnames += ("request",)
        strip_request = True

    if "event_loop" not in request.fixturenames:
        raise Exception("`event_loop` fixture required")

    # noinspection PyProtectedMember
    loop_fixturedef = request._get_active_fixturedef("event_loop")

    def wrapper(*args, **kwargs):  # type: ignore
        if strip_request:
            request = kwargs.pop("request")
        else:
            request = kwargs["request"]

        event_loop = request.getfixturevalue("event_loop")

        if not is_async_gen:
            return event_loop.run_until_complete(func(*args, **kwargs))

        gen = func(*args, **kwargs)

        def finalizer():  # type: ignore
            try:
                return event_loop.run_until_complete(gen.__anext__())
            except StopAsyncIteration:  # NOQA
                pass

        loop_fixturedef.addfinalizer(
            partial(fixturedef.finish, request=request),
        )

        request.addfinalizer(finalizer)
        return event_loop.run_until_complete(gen.__anext__())

    fixturedef.func = wrapper


@pytest.fixture(scope="session")
def localhost() -> str:
    params = (
        (socket.AF_INET, "127.0.0.1"),
        (socket.AF_INET6, "::1"),
    )
    for family, addr in params:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((addr, 0))
            except Exception:
                pass
            else:
                return addr
    raise RuntimeError("localhost unavailable")


@pytest.fixture(scope="session")
def loop_debug(pytestconfig: pytest.Config) -> bool:
    return pytestconfig.getoption("--aiomisc-debug")


@pytest.fixture(scope="session")
def aiomisc_test_timeout(
    pytestconfig: pytest.Config,
) -> Optional[Union[int, float]]:
    return pytestconfig.getoption("--aiomisc-test-timeout")


@pytest.fixture(autouse=True)
def aiomisc_func_wrap() -> Callable:
    return aiomisc.awaitable


def pytest_pycollect_makeitem(collector, name, obj):  # type: ignore
    if collector.funcnamefilter(name) and asyncio.iscoroutinefunction(obj):
        return list(collector._genfunctions(name, obj))


@pytest.mark.tryfirst
def pytest_pyfunc_call(pyfuncitem):  # type: ignore
    if not asyncio.iscoroutinefunction(pyfuncitem.function):
        return

    event_loop = pyfuncitem.funcargs.get("event_loop", None)
    func_wraper = pyfuncitem.funcargs.get(
        "aiomisc_func_wrap", aiomisc.awaitable,
    )

    aiomisc_test_timeout = pyfuncitem.funcargs.get(
        "aiomisc_test_timeout", None,
    )

    kwargs = {
        arg: pyfuncitem.funcargs[arg]
        for arg in pyfuncitem._fixtureinfo.argnames
    }

    @wraps(pyfuncitem.obj)
    async def func() -> Any:
        return await asyncio.wait_for(
            func_wraper(pyfuncitem.obj)(**kwargs),
            timeout=aiomisc_test_timeout,
        )

    event_loop.run_until_complete(func())

    return True


@pytest.fixture(scope="session")
def thread_pool_size(request: pytest.FixtureRequest) -> int:
    return request.config.getoption("--aiomisc-pool-size")


@pytest.fixture
def services() -> Iterable[aiomisc.Service]:
    return []


@pytest.fixture
def default_context() -> Mapping[str, Any]:
    return {}


loop_autouse = os.getenv("AIOMISC_LOOP_AUTOUSE", "1") == "1"


@pytest.fixture(name="thread_pool_executor")
def _thread_pool_executor() -> Type[concurrent.futures.ThreadPoolExecutor]:
    from aiomisc.thread_pool import ThreadPoolExecutor
    return ThreadPoolExecutor


@pytest.fixture(autouse=loop_autouse, name="event_loop_policy")
def _event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
    if USE_UVLOOP and uvloop_module is not None:
        return uvloop_module.EventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture(name="entrypoint_kwargs")
def _entrypoint_kwargs() -> dict:
    return {"log_config": False}


@pytest.fixture(autouse=loop_autouse)
def event_loop(
    request: pytest.FixtureRequest,
    event_loop_policy: asyncio.AbstractEventLoopPolicy,
    caplog: pytest.LogCaptureFixture,
    thread_pool_size: int,
    loop_debug: bool,
    thread_pool_executor: Callable[..., concurrent.futures.ThreadPoolExecutor],
) -> Generator[Any, asyncio.AbstractEventLoop, None]:
    basic_config(
        log_format="plain",
        stream=caplog.handler.stream,
    )

    get_marker = request.node.get_closest_marker
    forbid_loop_getter_marker = get_marker("forbid_get_event_loop")
    catch_unhandled_marker = get_marker("catch_loop_exceptions")

    exceptions: List[Dict[str, Any]] = []

    def catch_exceptions(_: Any, catched: Dict[str, Any]) -> None:
        nonlocal exceptions   # noqa
        exceptions.append(catched)

    try:
        asyncio.set_event_loop_policy(event_loop_policy)

        loop = asyncio.new_event_loop()
        loop.set_debug(loop_debug)

        asyncio.set_event_loop(loop)
        set_current_loop(loop)

        pool = thread_pool_executor(thread_pool_size)
        loop.set_default_executor(pool)

        if catch_unhandled_marker:
            loop.set_exception_handler(catch_exceptions)

        if LOG_LEVEL:
            LOG_LEVEL.set(logging.getLogger().getEffectiveLevel())

        try:
            with mock_get_event_loop() as event_loop_getter_mock:
                if forbid_loop_getter_marker:
                    event_loop_getter_mock.side_effect = partial(
                        pytest.fail, "get_event_loop is forbidden",
                    )
                yield loop
        finally:
            if exceptions:
                logging.error(
                    "Unhandled exceptions found:\n\n\t%s",
                    "\n\t".join(
                        (
                            "Message: {m}\n\t"
                            "Future: {f}\n\t"
                            "Exception: {e}"
                        ).format(
                            m=e["message"],
                            f=repr(e.get("future")),
                            e=repr(e.get("exception")),
                        ) for e in exceptions
                    ),
                )
                pytest.fail("Unhandled exceptions found. See logs.")

            basic_config(
                log_format="plain",
                stream=sys.stderr,
            )

            if loop.is_closed():
                return

            with suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
            with suppress(Exception):
                loop.close()
    finally:
        asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())


@pytest.fixture
def loop(event_loop: asyncio.AbstractEventLoop) -> asyncio.AbstractEventLoop:
    warnings.warn("fixture `loop` is deprecated, use `event_loop` instead")
    return event_loop


@pytest.fixture(autouse=loop_autouse)
async def entrypoint(
    services: Iterable[aiomisc.Service],
    loop_debug: bool,
    default_context: Mapping[str, Any],
    entrypoint_kwargs: Mapping[str, Any],
    thread_pool_size: int,
    thread_pool_executor: Callable[..., concurrent.futures.ThreadPoolExecutor],
    event_loop: asyncio.AbstractEventLoop,
) -> AsyncGenerator[aiomisc.entrypoint, None]:
    from aiomisc.context import get_context
    from aiomisc.entrypoint import entrypoint

    async with entrypoint(
        *services, loop=event_loop, **entrypoint_kwargs
    ) as ep:
        ctx = get_context()
        for key, value in default_context.items():
            ctx[key] = value
        yield ep


def get_unused_port(*args: Any) -> int:
    warnings.warn(
        (
            "Do not use get_unused_port directly, use fixture "
            "'aiomisc_unused_port_factory'"
        ),
        DeprecationWarning, stacklevel=2,
    )
    return unused_port(*args)


class PortSocket(NamedTuple):
    port: int
    socket: socket.socket


@pytest.fixture
def aiomisc_socket_factory(
    request: pytest.FixtureRequest, localhost: str,
) -> Callable[..., PortSocket]:
    """ Returns a """
    def factory(*args: Any, **kwargs: Any) -> PortSocket:
        sock = bind_socket(*args, address=localhost, port=0, **kwargs)
        port = sock.getsockname()[1]

        # Close socket after teardown
        request.addfinalizer(sock.close)

        return PortSocket(port=port, socket=sock)
    return factory


class SocketWrapper(abc.ABC):
    address: str
    port: int

    def __init__(self, *args: Any):
        self._socket_args: Tuple[Any, ...] = args
        self.address: str = ""
        self.port: int = 0

    @abc.abstractmethod
    def prepare(self, address: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class SocketWrapperUnix(SocketWrapper):
    socket: socket.socket
    fd: int

    def __init__(self, *args: Any):
        super().__init__(*args)
        self.socket = socket.socket(*args)
        self.fd = -1

    def close(self) -> None:
        self.socket.close()
        if self.fd > 0:
            os.close(self.fd)

    def prepare(self, address: str) -> None:
        self.socket.bind((address, 0))
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock_set_reuseport(self.socket, True)
        self.address, self.port = self.socket.getsockname()[:2]
        # detach socket object and save file descriptor
        self.fd = self.socket.detach()


class SocketWrapperWindows(SocketWrapper):
    def prepare(self, address: str) -> None:
        self.address = address
        self.port = unused_port(*self._socket_args)


socket_wrapper: Type[SocketWrapper]

if platform.system() == "Windows":
    socket_wrapper = SocketWrapperWindows
else:
    socket_wrapper = SocketWrapperUnix


@pytest.fixture
def aiomisc_unused_port_factory(
    request: pytest.FixtureRequest, localhost: str,
) -> Callable[[], int]:
    def port_factory(*args: Any) -> int:
        wrapper = socket_wrapper(*args)
        wrapper.prepare("::" if ":" in localhost else "0.0.0.0")
        request.addfinalizer(wrapper.close)
        return wrapper.port
    return port_factory


@pytest.fixture
def aiomisc_unused_port(aiomisc_unused_port_factory: Callable[..., int]) -> int:
    return aiomisc_unused_port_factory()
