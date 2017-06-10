import inspect
import asyncio

from functools import wraps

from lru import LRU

# TODO: different cache strategies

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

def lru_cache(maxsize=128):
    def decorator(func):
        _internal_cache = LRU(maxsize)

        def _make_key(*args, **kwargs):
            return f'{func.__module__}.{func.__name__}#{repr(args)}##{repr(kwargs)}'

        @wraps(func)
        def wrapper(*args, **kwargs):
            key = _make_key(*args, **kwargs)
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
                del _internal_cache[_make_key(*args, **kwargs)]
            except KeyError:
                pass

        wrapper.cache = _internal_cache
        wrapper.get_key = _make_key
        wrapper.invalidate = _invalidate
        return wrapper
    return decorator
