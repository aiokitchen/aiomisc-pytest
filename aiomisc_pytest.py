import pytest

# Re-export everything from aiomisc.pytest
from aiomisc.pytest import *  # noqa: F401, F403
from aiomisc.pytest import __all__  # noqa: F401


# Pytest plugin hooks (must be here for pytest to find them via entry point)
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
        "--aiomisc-debug",
        action="store_true",
        default=False,
        help="Set debug for event loop",
    )

    group.addoption(
        "--aiomisc-pool-size",
        type=int,
        default=4,
        help="Default thread pool size",
    )

    group.addoption(
        "--aiomisc-test-timeout",
        type=float,
        default=None,
        help="Test timeout",
    )
