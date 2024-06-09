import asyncio
import os
import threading
from types import TracebackType
from typing import (
    Any,
    Callable,
    Coroutine,
    Literal,
    Optional,
    Protocol,
    Type,
    TypeVar,
    cast,
)

from ._exceptions import ExceptionMapping, PoolTimeout, map_exceptions

# Our async synchronization primitives use either 'asyncio' or 'trio' depending
# on if they're running under asyncio or trio.

try:
    import trio
except ImportError:  # pragma: nocover
    trio = None  # type: ignore


try:
    import anyio
except ImportError:  # pragma: nocover
    anyio = None  # type: ignore


AsyncBackend = Literal["asyncio", "trio"]
AsyncLibrary = Literal["asyncio", "trio", "anyio"]


def current_async_backend() -> AsyncBackend:
    # Determine if we're running under trio or asyncio.
    # See https://sniffio.readthedocs.io/en/latest/
    try:
        import sniffio
    except ImportError:  # pragma: nocover
        environment: AsyncBackend = "asyncio"
    else:
        environment = cast(AsyncBackend, sniffio.current_async_library())

    if environment not in ("asyncio", "trio"):  # pragma: nocover
        raise RuntimeError("Running under an unsupported async environment.")

    if environment == "trio" and trio is None:  # pragma: nocover
        raise RuntimeError(
            "Running with trio requires installation of 'httpcore[trio]'."
        )

    return environment


def current_async_library() -> AsyncLibrary:
    if current_async_backend() == "trio":
        return "trio"

    if anyio is not None:
        anyio_env = os.environ.get("HTTPCORE_PREFER_ANYIO", "true").lower()
        if anyio_env in ("true", "1"):
            return "anyio"

    return "asyncio"


class _LockProto(Protocol):
    async def acquire(self) -> Any: ...
    def release(self) -> None: ...


class _EventProto(Protocol):
    def set(self) -> None: ...
    async def wait(self) -> Any: ...


class AsyncLock:
    """
    This is a standard lock.

    In the sync case `Lock` provides thread locking.
    In the async case `AsyncLock` provides async locking.
    """

    def __init__(self) -> None:
        self._lock: Optional[_LockProto] = None

    def setup(self) -> _LockProto:
        """
        Detect if we're running under 'asyncio' or 'trio' and create
        a lock with the correct implementation.
        """
        if current_async_backend() == "trio":
            lock: _LockProto = trio.Lock()
        else:
            # Note: asyncio.Lock has better performance characteristics than anyio.Lock
            # https://github.com/encode/httpx/issues/3215
            lock = asyncio.Lock()
        self._lock = lock
        return lock

    async def __aenter__(self) -> "AsyncLock":
        if (lock := self._lock) is None:
            lock = self.setup()
        await lock.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]] = None,
        exc_value: Optional[BaseException] = None,
        traceback: Optional[TracebackType] = None,
    ) -> None:
        cast(_LockProto, self._lock).release()


class AsyncThreadLock:
    """
    This is a threading-only lock for no-I/O contexts.

    In the sync case `ThreadLock` provides thread locking.
    In the async case `AsyncThreadLock` is a no-op.
    """

    def __enter__(self) -> "AsyncThreadLock":
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]] = None,
        exc_value: Optional[BaseException] = None,
        traceback: Optional[TracebackType] = None,
    ) -> None:
        pass


class AsyncEvent:
    def __init__(self) -> None:
        self._backend = ""
        self._event: Optional[_EventProto] = None

    def setup(self) -> _EventProto:
        """
        Detect if we're running under 'asyncio' or 'trio' and create
        a lock with the correct implementation.
        """
        self._backend = current_async_backend()
        if self._backend == "trio":
            event: _EventProto = trio.Event()
        else:
            # Note: asyncio.Event has better performance characteristics than anyio.Event
            event = asyncio.Event()
        self._event = event
        return event

    def set(self) -> None:
        if (event := self._event) is None:
            event = self.setup()
        event.set()

    async def wait(self, timeout: Optional[float] = None) -> None:
        if (event := self._event) is None:
            event = self.setup()

        if self._backend == "trio":
            trio_exc_map: ExceptionMapping = {trio.TooSlowError: PoolTimeout}
            timeout_or_inf = float("inf") if timeout is None else timeout
            with map_exceptions(trio_exc_map):
                with trio.fail_after(timeout_or_inf):
                    await event.wait()
        else:
            asyncio_exc_map: ExceptionMapping = {TimeoutError: PoolTimeout}
            with map_exceptions(asyncio_exc_map):
                async with asyncio.timeout(timeout):
                    await event.wait()


class AsyncSemaphore:
    def __init__(self, bound: int) -> None:
        self._bound = bound
        self._semaphore: Optional[_LockProto] = None

    def setup(self) -> _LockProto:
        """
        Detect if we're running under 'asyncio' or 'trio' and create
        a semaphore with the correct implementation.
        """
        if current_async_backend() == "trio":
            semaphore: _LockProto = trio.Semaphore(
                initial_value=self._bound, max_value=self._bound
            )
        else:
            # Note: asyncio.BoundedSemaphore has better performance characteristics than anyio.Semaphore
            semaphore = asyncio.BoundedSemaphore(self._bound)
        self._semaphore = semaphore
        return semaphore

    async def acquire(self) -> None:
        if (semaphore := self._semaphore) is None:
            semaphore = self.setup()
        await semaphore.acquire()

    async def release(self) -> None:
        cast(_LockProto, self._semaphore).release()


T = TypeVar("T")


class AsyncShieldCancellation:
    # For certain portions of our codebase where we're dealing with
    # closing connections during exception handling we want to shield
    # the operation from being cancelled.
    #
    # async def cleanup():
    #     ...
    #
    # # clean-up operations, shielded from cancellation.
    # await AsyncShieldCancellation.shield(cleanup)

    @staticmethod
    async def shield(shielded: Callable[[], Coroutine[Any, Any, None]]) -> None:
        if current_async_backend() == "trio":
            with trio.CancelScope(shield=True):
                await shielded()
        else:
            await AsyncShieldCancellation._asyncio_shield(shielded)

    @staticmethod
    async def _asyncio_shield(
        shielded: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        inner_task = asyncio.create_task(shielded())
        try:
            await asyncio.shield(inner_task)
        except asyncio.CancelledError:
            # Let the inner_task to complete as it was shielded from the cancellation
            await inner_task


# Our thread-based synchronization primitives...


class Lock:
    """
    This is a standard lock.

    In the sync case `Lock` provides thread locking.
    In the async case `AsyncLock` provides async locking.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def __enter__(self) -> "Lock":
        self._lock.acquire()
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]] = None,
        exc_value: Optional[BaseException] = None,
        traceback: Optional[TracebackType] = None,
    ) -> None:
        self._lock.release()


class ThreadLock:
    """
    This is a threading-only lock for no-I/O contexts.

    In the sync case `ThreadLock` provides thread locking.
    In the async case `AsyncThreadLock` is a no-op.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def __enter__(self) -> "ThreadLock":
        self._lock.acquire()
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]] = None,
        exc_value: Optional[BaseException] = None,
        traceback: Optional[TracebackType] = None,
    ) -> None:
        self._lock.release()


class Event:
    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        self._event.set()

    def wait(self, timeout: Optional[float] = None) -> None:
        if timeout == float("inf"):  # pragma: no cover
            timeout = None
        if not self._event.wait(timeout=timeout):
            raise PoolTimeout()  # pragma: nocover


class Semaphore:
    def __init__(self, bound: int) -> None:
        self._semaphore = threading.Semaphore(value=bound)

    def acquire(self) -> None:
        self._semaphore.acquire()

    def release(self) -> None:
        self._semaphore.release()


class ShieldCancellation:
    # Thread-synchronous codebases don't support cancellation semantics.
    # We have this class because we need to mirror the async and sync
    # cases within our package, but it's just a no-op.
    @staticmethod
    def shield(fn: Callable[[], None]) -> None:
        fn()
