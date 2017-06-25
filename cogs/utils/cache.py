import inspect
import asyncio
import enum

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

class Strategy(enum.Enum):
    lru = 1
    raw = 2

def cache(maxsize=128, strategy=Strategy.lru):
    def decorator(func):
        if strategy is Strategy.lru:
            _internal_cache = LRU(maxsize)
            _stats = _internal_cache.get_stats
        elif strategy is Strategy.raw:
            _internal_cache = {}
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
            for k, v in kwargs.items():
                # note: this only really works for this use case in particular
                # I want to pass asyncpg.Connection objects to the parameters
                # however, they use default __repr__ and I do not care what
                # connection is passed in, so I needed a bypass.
                if k == 'connection':
                    continue

                key.append(_true_repr(k))
                key.append(_true_repr(v))

            return ''.join(key)

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

        wrapper.cache = _internal_cache
        wrapper.get_key = lambda *args, **kwargs: _make_key(args, kwargs)
        wrapper.invalidate = _invalidate
        wrapper.get_stats = _stats
        return wrapper
    return decorator
