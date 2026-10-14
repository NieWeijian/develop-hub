from __future__ import annotations

from contextlib import asynccontextmanager
import os

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import router
from .codex_runner import CodexRunner
from .config import settings
from .db import initialize_database
from .events import broker
from .qa_api import router as qa_router
from .qa_demo_api import router as qa_demo_router
from .qa_runner import RemoteQaRunner
from .qa_plugins import QaPluginRegistry
from .qa_workflow import QaWorkflow
from .test_workbench import TestRoundWorkflow
from .test_workbench_api import router as test_workbench_router
from .workflow import CodingWorkflow
from .testing_platform import router as testing_platform_router, ScheduleEngine
from .special_scenarios import router as special_router, SpecialWorkflow
from .testing.api import router as testing_router
from .testing.resources_api import router as testing_resources_router
from .testing.runner import TestingRunner

LEGACY_ENABLED = os.getenv('DEVELOP_HUB_LEGACY_ENABLED', 'false').lower() == 'true'


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    from .testing.safety import validate_isolated_paths
    validate_isolated_paths(settings)
    settings.ensure_directories()
    if settings.auto_create_schema:
        initialize_database()
    testing_runner = TestingRunner(settings)
    app.state.testing_runner = testing_runner
    await testing_runner.recover()
    if not LEGACY_ENABLED:
        try:
            yield
        finally:
            await testing_runner.stop()
        return
    runner = CodexRunner(settings, broker)
    remote_qa_runner = RemoteQaRunner(settings)
    qa_plugins = QaPluginRegistry(settings, remote_qa_runner)
    workflow = CodingWorkflow(settings, runner, broker, qa_plugins)
    app.state.workflow = workflow
    app.state.qa_plugins = qa_plugins
    await workflow.recover_interrupted_runs()
    qa_workflow = QaWorkflow(settings, remote_qa_runner)
    app.state.qa_workflow = qa_workflow
    await qa_workflow.recover_interrupted_runs()
    test_round_workflow = TestRoundWorkflow(settings)
    app.state.test_round_workflow = test_round_workflow
    await test_round_workflow.recover_interrupted_rounds()
    special_workflow = SpecialWorkflow(test_round_workflow)
    app.state.special_workflow = special_workflow
    await special_workflow.recover()
    scheduler = ScheduleEngine(test_round_workflow)
    app.state.test_scheduler = scheduler
    scheduler.start()
    try:
        yield
    finally:
        await testing_runner.stop()
        await scheduler.stop()
        await special_workflow.stop()


app = FastAPI(
    title="Develop Hub API",
    version="0.1.0",
    description="Configurable testing plans for Rockchip devices and firmware",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(testing_router)
app.include_router(testing_resources_router)
if LEGACY_ENABLED:
    app.include_router(router)
    app.include_router(qa_router)
    app.include_router(qa_demo_router)
    app.include_router(test_workbench_router)
    app.include_router(testing_platform_router)
    app.include_router(special_router)
else:
    @app.get('/api/health')
    def testing_health():
        from .testing.safety import simulation_only
        return {'status': 'ok', 'service': 'rk-testing', 'legacy_enabled': False, 'environment': settings.environment, 'device_mode': 'simulation-only' if simulation_only() else 'enabled'}


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "develop-hub", "docs": "/docs", "health": "/api/health"}


def run() -> None:
    uvicorn.run("develop_hub.main:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    run()
