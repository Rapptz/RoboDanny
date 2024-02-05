from __future__ import annotations
import asyncio
from collections import deque, OrderedDict
from typing import Generic, Optional, TypeVar

K = TypeVar('K')
V = TypeVar('V')


class CancellableQueue(Generic[K, V]):
    """A queue that lets you cancel the items pending for work by a provided unique ID."""

    def __init__(self) -> None:
        self._waiters: deque[asyncio.Future[None]] = deque()
        self._data: OrderedDict[K, V] = OrderedDict()
        self._loop = asyncio.get_running_loop()

    def __wakeup_next(self) -> None:
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                break

    def __repr__(self) -> str:
        return f'<{self.__class__.__name__} data={self._data!r} getters[{len(self._waiters)}]>'

    def __len__(self) -> int:
        return len(self._data)

    def is_empty(self) -> bool:
        """:class:`bool`: Returns ``True`` if the queue is empty."""
        return not self._data

    def put(self, key: K, value: V) -> None:
        """Puts an item into the queue.

        If the key is the same as one that already exists then it's overwritten.

        This wakes up the first coroutine waiting for the queue.
        """

        self._data[key] = value
        self.__wakeup_next()

    async def get(self) -> V:
        """Removes and returns an item from the queue.

        If the queue is empty then it waits until one is available.
        """

        while self.is_empty():
            getter = self._loop.create_future()
            self._waiters.append(getter)

            try:
                await getter
            except:
                getter.cancel()
                try:
                    self._waiters.remove(getter)
                except ValueError:
                    pass

                if not self.is_empty() and not getter.cancelled():
                    self.__wakeup_next()

                raise

        _, value = self._data.popitem(last=False)
        return value

    def is_pending(self, key: K) -> bool:
        """Returns ``True`` if the key is currently pending in the queue."""
        return key in self._data

    def cancel(self, key: K) -> Optional[V]:
        """Attempts to cancel the queue item at the given key and returns it if so."""
        return self._data.pop(key, None)

    def cancel_all(self) -> None:
        """Cancels all the queue items"""
        self._data.clear()
