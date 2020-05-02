import gc
import pytest
import trio
from async_generator import asynccontextmanager
from greenback import autoawait, async_context, async_iter, ensure_portal

@autoawait
async def sync_sleep(n):
    await trio.sleep(n)

async def test_autoawait():
    await ensure_portal()
    sync_sleep(1)
    await trio.sleep(2)
    sync_sleep(3)
    assert trio.current_time() == 6

class AsyncMagic:
    @autoawait
    async def __init__(self):
        await trio.sleep(1)

    @autoawait
    async def __del__(self):
        await trio.sleep(2)

async def test_magic():
    await ensure_portal()

    async def construct_and_maybe_destroy():
        await ensure_portal()
        AsyncMagic()

    async with trio.open_nursery() as nursery:
        nursery.start_soon(construct_and_maybe_destroy)
        obj = AsyncMagic()
    del obj
    for _ in range(4):
        gc.collect()

    # If the child task's object's __del__ ran before the child
    # task exited, the nursery took 3 sec and the del+GC took 2.
    # If not (eg on PyPy), the nursery took 1 sec and the del+GC
    # took 4. Regardless the total should be 5 fake seconds.
    assert trio.current_time() == 5

async def test_async_context():
    async def validate_against_native(async_cm_factory):
        async def summarize_in(template):
            resobj = "<failed>"
            try:
                async with template() as resobj:
                    pass
                res = f"got {resobj!r}"
            except BaseException as ex:
                if resobj == "<failed>":
                    res = f"failed in enter: {ex!r}"
                else:
                    res = f"got {resobj!r} then failed in exit: {ex!r}"

        @asynccontextmanager
        async def greenbacked_template():
            with async_context(async_cm_factory()) as res:
                yield res

        res_native = await summarize_in(async_cm_factory)
        res_gb = await summarize_in(greenbacked_template)

        assert res_native == res_gb

    class NoExitCM:
        async def __aenter__(self):
            await trio.sleep(1)  # pragma: no cover

    class NoEnterCM:
        async def __aexit__(self, *exc):
            await trio.sleep(1)  # pragma: no cover

    class ThrowExitCM:
        async def __aenter__(self):
            await trio.sleep(1)
        async def __aexit__(self, *exc):
            await trio.sleep(1)
            raise ValueError

    class InstanceAttrCM:
        def __init__(self):
            self.__aenter__ = lambda: "instance"  # pragma: no cover
            self.__aexit__ = None
        async def __aenter__(self):
            await trio.sleep(2)
            return "class"
        async def __aexit__(self, *exc):
            await trio.sleep(5)

    await ensure_portal()
    await validate_against_native(NoExitCM)
    await validate_against_native(NoEnterCM)
    await validate_against_native(ThrowExitCM)
    await validate_against_native(InstanceAttrCM)
    assert trio.current_time() == 18

async def test_async_iter():
    async def validate_against_native(async_iterable_factory):
        async def summarize_in(template):
            results = []
            try:
                async for val in template():
                    results.append(val)
            except BaseException as ex:
                print(repr(ex))
                results.append(f"exception: {ex!r}")
            return results

        async def greenbacked_template():
            for val in async_iter(async_iterable_factory()):
                yield val

        res_native = await summarize_in(async_iterable_factory)
        res_gb = await summarize_in(greenbacked_template)

        assert repr(res_native).replace("async for", "async_iter") == repr(res_gb)

    async def arange(end=10):
        for val in range(end):
            await trio.sleep(val)
            yield val

    class NoAIter:
        async def __anext__(self):
            raise StopAsyncIteration  # pragma: no cover

    class NoANext:
        def __aiter__(self):
            return self

    class IteratorDefinedElsewhere:
        def __aiter__(self):
            return arange(3)

        async def __anext__(self):
            raise ValueError("nope")  # pragma: no cover

    class InstanceAttrIter:
        def __init__(self):
            self.__aiter__ = arange
        def __aiter__(self):
            return self
        async def __anext__(self):
            raise StopAsyncIteration

    await ensure_portal()
    await validate_against_native(arange)
    await validate_against_native(NoAIter)
    await validate_against_native(NoANext)
    await validate_against_native(IteratorDefinedElsewhere)
    await validate_against_native(InstanceAttrIter)
    assert trio.current_time() == 96

async def test_async_iter_send_and_throw():
    await ensure_portal()

    async def example(suppress=False):
        try:
            yield (yield 10) + 1
        except BaseException as ex:
            if not suppress:
                raise

    def wrap_example(**kw):
        yield from async_iter(example(**kw))

    gi = wrap_example()
    assert gi.__next__() == 10
    assert gi.send(20) == 21
    with pytest.raises(ValueError, match="hi"):
        gi.throw(ValueError("hi"))

    gi = wrap_example()
    assert gi.send(None) == 10
    gi.close()

    gi = wrap_example(suppress=True)
    assert gi.send(None) == 10
    with pytest.raises(StopIteration):
        gi.throw(ValueError("lo"))

    gi = wrap_example()
    assert gi.__next__() == 10
    assert gi.send(20) == 21
    with pytest.raises(StopIteration):
        gi.send(30)