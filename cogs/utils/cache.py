import inspect
import asyncio
import enum
import time

from functools import wraps

from lru import LRU

def _wrap_and_store_coroutine(cache, key, coro):
    async def func():
        value = await coro
        cache[key] = value
        return value
    return func()

def _wrap_new_coroutine(value):
    async def new_coroutine():
        return value
    return new_coroutine()

class ExpiringCache(dict):
    def __init__(self, seconds):
        self.__ttl = seconds
        super().__init__()

    def __verify_cache_integrity(self):
        # Have to do this in two steps...
        current_time = time.monotonic()
        to_remove = [k for (k, (v, t)) in self.items() if current_time > (t + self.__ttl)]
        for k in to_remove:
            del self[k]

    def __getitem__(self, key):
        self.__verify_cache_integrity()
        return super().__getitem__(key)

    def __setitem__(self, key, value):
        super().__setitem__(key, (value, time.monotonic()))

class Strategy(enum.Enum):
    lru = 1
    raw = 2
    timed = 3

def cache(maxsize=128, strategy=Strategy.lru, ignore_kwargs=False):
    def decorator(func):
        if strategy is Strategy.lru:
            _internal_cache = LRU(maxsize)
            _stats = _internal_cache.get_stats
        elif strategy is Strategy.raw:
            _internal_cache = {}
            _stats = lambda: (0, 0)
        elif strategy is Strategy.timed:
            _internal_cache = ExpiringCache(maxsize)
            _stats = lambda: (0, 0)

        def _make_key(args, kwargs):
            # this is a bit of a cluster fuck
            # we do care what 'self' parameter is when we __repr__ it
            def _true_repr(o):
                if o.__class__.__repr__ is object.__repr__:
                    return f'<{o.__class__.__module__}.{o.__class__.__name__}>'
                return repr(o)

            key = [ f'{func.__module__}.{func.__name__}' ]
            key.extend(_true_repr(o) for o in args)
            if not ignore_kwargs:
                for k, v in kwargs.items():
                    # note: this only really works for this use case in particular
                    # I want to pass asyncpg.Connection objects to the parameters
                    # however, they use default __repr__ and I do not care what
                    # connection is passed in, so I needed a bypass.
                    if k == 'connection':
                        continue

                    key.append(_true_repr(k))
                    key.append(_true_repr(v))

            return ':'.join(key)

        @wraps(func)
        def wrapper(*args, **kwargs):
            key = _make_key(args, kwargs)
            try:
                value = _internal_cache[key]
            except KeyError:
                value = func(*args, **kwargs)

                if inspect.isawaitable(value):
                    return _wrap_and_store_coroutine(_internal_cache, key, value)

                _internal_cache[key] = value
                return value
            else:
                if asyncio.iscoroutinefunction(func):
                    return _wrap_new_coroutine(value)
                return value

        def _invalidate(*args, **kwargs):
            try:
                del _internal_cache[_make_key(args, kwargs)]
            except KeyError:
                return False
            else:
                return True

        def _invalidate_containing(key):
            to_remove = []
            for k in _internal_cache.keys():
                if key in k:
                    to_remove.append(k)
            for k in to_remove:
                try:
                    del _internal_cache[k]
                except KeyError:
                    continue

        wrapper.cache = _internal_cache
        wrapper.get_key = lambda *args, **kwargs: _make_key(args, kwargs)
        wrapper.invalidate = _invalidate
        wrapper.get_stats = _stats
        wrapper.invalidate_containing = _invalidate_containing
        return wrapper
    return decorator
