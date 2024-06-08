import asyncio
import threading
from types import TracebackType
from typing import Any, Callable, Coroutine, Optional, Type, TypeVar

from ._exceptions import ExceptionMapping, PoolTimeout, map_exceptions

# Our async synchronization primitives use either 'asyncio' or 'trio' depending
# on if they're running under asyncio or trio.

try:
    import trio
except ImportError:  # pragma: nocover
    trio = None  # type: ignore


def current_async_library() -> str:
    # Determine if we're running under trio or asyncio.
    # See https://sniffio.readthedocs.io/en/latest/
    try:
        import sniffio
    except ImportError:  # pragma: nocover
        environment = "asyncio"
    else:
        environment = sniffio.current_async_library()

    if environment not in ("asyncio", "trio"):  # pragma: nocover
        raise RuntimeError("Running under an unsupported async environment.")

    if environment == "trio" and trio is None:  # pragma: nocover
        raise RuntimeError(
            "Running with trio requires installation of 'httpcore[trio]'."
        )

    return environment


class AsyncLock:
    """
    This is a standard lock.

    In the sync case `Lock` provides thread locking.
    In the async case `AsyncLock` provides async locking.
    """

    def __init__(self) -> None:
        self._backend = ""

    def setup(self) -> None:
        """
        Detect if we're running under 'asyncio' or 'trio' and create
        a lock with the correct implementation.
        """
        self._backend = current_async_library()
        if self._backend == "trio":
            self._trio_lock = trio.Lock()
        elif self._backend == "asyncio":
            # asyncio.Lock has better performance characteristics than anyio.Lock
            # https://github.com/encode/httpx/issues/3215
            self._asyncio_lock = asyncio.Lock()

    async def __aenter__(self) -> "AsyncLock":
        if not self._backend:
            self.setup()

        if self._backend == "trio":
            await self._trio_lock.acquire()
        elif self._backend == "asyncio":
            await self._asyncio_lock.acquire()

        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]] = None,
        exc_value: Optional[BaseException] = None,
        traceback: Optional[TracebackType] = None,
    ) -> None:
        if self._backend == "trio":
            self._trio_lock.release()
        elif self._backend == "asyncio":
            self._asyncio_lock.release()


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

    def setup(self) -> None:
        """
        Detect if we're running under 'asyncio' or 'trio' and create
        a lock with the correct implementation.
        """
        self._backend = current_async_library()
        if self._backend == "trio":
            self._trio_event = trio.Event()
        elif self._backend == "asyncio":
            # asyncio.Event has better performance characteristics than anyio.Event
            self._asyncio_event = asyncio.Event()

    def set(self) -> None:
        if not self._backend:
            self.setup()

        if self._backend == "trio":
            self._trio_event.set()
        elif self._backend == "asyncio":
            self._asyncio_event.set()

    async def wait(self, timeout: Optional[float] = None) -> None:
        if not self._backend:
            self.setup()

        if self._backend == "trio":
            trio_exc_map: ExceptionMapping = {trio.TooSlowError: PoolTimeout}
            timeout_or_inf = float("inf") if timeout is None else timeout
            with map_exceptions(trio_exc_map):
                with trio.fail_after(timeout_or_inf):
                    await self._trio_event.wait()
        elif self._backend == "asyncio":
            asyncio_exc_map: ExceptionMapping = {TimeoutError: PoolTimeout}
            with map_exceptions(asyncio_exc_map):
                async with asyncio.timeout(timeout):
                    await self._asyncio_event.wait()


class AsyncSemaphore:
    def __init__(self, bound: int) -> None:
        self._bound = bound
        self._backend = ""

    def setup(self) -> None:
        """
        Detect if we're running under 'asyncio' or 'trio' and create
        a semaphore with the correct implementation.
        """
        self._backend = current_async_library()
        if self._backend == "trio":
            self._trio_semaphore = trio.Semaphore(
                initial_value=self._bound, max_value=self._bound
            )
        elif self._backend == "asyncio":
            # asyncio.Semaphore has better performance characteristics than anyio.Semaphore
            self._asyncio_semaphore = asyncio.Semaphore(value=self._bound)

    async def acquire(self) -> None:
        if not self._backend:
            self.setup()

        if self._backend == "trio":
            await self._trio_semaphore.acquire()
        elif self._backend == "asyncio":
            await self._asyncio_semaphore.acquire()

    async def release(self) -> None:
        if self._backend == "trio":
            self._trio_semaphore.release()
        elif self._backend == "asyncio":
            self._asyncio_semaphore.release()


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
        backend = current_async_library()
        if backend == "trio":
            await AsyncShieldCancellation._trio_shield(shielded)
        elif backend == "asyncio":
            await AsyncShieldCancellation._asyncio_shield(shielded)

    @staticmethod
    async def _trio_shield(shielded: Callable[[], Coroutine[Any, Any, None]]) -> None:
        with trio.CancelScope(shield=True):
            await shielded()

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
