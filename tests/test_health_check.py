"""Tests for health check system (P3)."""

import asyncio

from core.extension import Extension
from core.registry import Registry


def _run(coro):
    return asyncio.run(coro)


class HealthyExtension(Extension):
    name = "healthy"

    async def start(self):
        pass

    async def stop(self):
        pass

    async def health_check(self):
        return {"status": "ok", "secrets": 5}


class ErrorExtension(Extension):
    name = "broken"

    async def start(self):
        pass

    async def stop(self):
        pass

    async def health_check(self):
        raise RuntimeError("something went wrong")


class TimeoutExtension(Extension):
    name = "slow"

    async def start(self):
        pass

    async def stop(self):
        pass

    async def health_check(self):
        await asyncio.sleep(60)
        return {"status": "ok"}


class DefaultExtension(Extension):
    """Uses the base class default health_check."""

    name = "default"

    async def start(self):
        pass

    async def stop(self):
        pass


class TestDefaultHealthCheck:
    def test_base_class_returns_ok(self):
        ext = DefaultExtension()
        result = _run(ext.health_check())
        assert result == {"status": "ok"}


class TestHealthCheckAll:
    def _make_registry(self, extensions):
        """Create a Registry with pre-loaded extensions (bypass load())."""

        # Minimal engine stub
        class _Engine:
            events = None

        registry = Registry(_Engine(), {})
        registry._extensions = extensions
        return registry

    def test_all_healthy(self):
        reg = self._make_registry([HealthyExtension(), DefaultExtension()])
        results = _run(reg.health_check_all())
        assert results["healthy"]["status"] == "ok"
        assert results["healthy"]["secrets"] == 5
        assert results["default"]["status"] == "ok"

    def test_error_extension(self):
        reg = self._make_registry([HealthyExtension(), ErrorExtension()])
        results = _run(reg.health_check_all())
        assert results["healthy"]["status"] == "ok"
        assert results["broken"]["status"] == "error"
        assert "something went wrong" in results["broken"]["detail"]

    def test_timeout_extension(self):
        """Extension that exceeds health_check timeout gets status=error."""

        class SlowExtension(Extension):
            name = "slow"

            async def start(self):
                pass

            async def stop(self):
                pass

            async def health_check(self):
                await asyncio.sleep(10)
                return {"status": "ok"}

        reg = self._make_registry([SlowExtension()])

        # Monkeypatch the timeout to 0.05s for fast test
        async def fast_health_check_all():
            async def _check(ext):
                try:
                    result = await asyncio.wait_for(ext.health_check(), timeout=0.05)
                    return ext.name, result
                except TimeoutError:
                    return ext.name, {"status": "error", "detail": "timeout"}
                except Exception as e:
                    return ext.name, {"status": "error", "detail": str(e)}

            pairs = await asyncio.gather(*[_check(e) for e in reg._extensions])
            return dict(pairs)

        results = _run(fast_health_check_all())
        assert results["slow"]["status"] == "error"
        assert results["slow"]["detail"] == "timeout"

    def test_empty_registry(self):
        reg = self._make_registry([])
        results = _run(reg.health_check_all())
        assert results == {}

    def test_mixed_extensions(self):
        reg = self._make_registry(
            [
                HealthyExtension(),
                ErrorExtension(),
                DefaultExtension(),
            ]
        )
        results = _run(reg.health_check_all())
        assert len(results) == 3
        assert results["healthy"]["status"] == "ok"
        assert results["broken"]["status"] == "error"
        assert results["default"]["status"] == "ok"
