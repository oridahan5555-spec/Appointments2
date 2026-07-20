import asyncio

import app as app_module
import config


def test_lifespan_handles_startup_errors_without_raising(monkeypatch):
    def boom() -> None:
        raise RuntimeError("startup failed")

    monkeypatch.setattr(config, "validate_runtime_config", boom)
    monkeypatch.setattr(app_module.db, "init_db", lambda: None)

    async def run_lifespan() -> None:
        async with app_module.app.router.lifespan_context(app_module.app):
            assert isinstance(app_module.app.state.startup_error, RuntimeError)
            assert "startup failed" in str(app_module.app.state.startup_error)

    asyncio.run(run_lifespan())
