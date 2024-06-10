import os
import typing
from typing import Generator, List

import pytest

from httpcore import AnyIOBackend, AsyncioBackend, TrioBackend
from httpcore._backends.auto import AutoBackend
from httpcore._synchronization import AsyncLibrary, current_async_library


@pytest.fixture(scope="session", autouse=True)
def check_tested_async_libraries() -> Generator[List[str], None, None]:
    # Ensure tests cover all supported async variants
    async_libraries: List[str] = []
    yield async_libraries
    expected = sorted(["asyncio", "anyio", "trio"])
    assert sorted(async_libraries) == expected
    assert sorted(typing.get_args(AsyncLibrary)) == expected


@pytest.mark.anyio
async def test_current_async_library(anyio_backend, check_tested_async_libraries):
    current = current_async_library()
    check_tested_async_libraries.append(current)

    backend_name, _ = anyio_backend
    auto_backend = AutoBackend()
    await auto_backend._init_backend()

    if backend_name == "trio":
        assert current == "trio"
        assert isinstance(auto_backend._backend, TrioBackend)
    else:
        assert backend_name == "asyncio"
        if os.environ["HTTPCORE_PREFER_ANYIO"] == "1":
            assert current == "anyio"
            assert isinstance(auto_backend._backend, AnyIOBackend)
        else:
            assert os.environ["HTTPCORE_PREFER_ANYIO"] == "0"
            assert current == "asyncio"
            assert isinstance(auto_backend._backend, AsyncioBackend)
