from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .routers import operations, reservations, resources, search, workorders

app = FastAPI(
    title=settings.app_name,
    version="2.0.0",
    description=(
        "充电预约 + 运维工单一体化演示：固定枪口/资源池两种预约粒度、事务级容量重算、"
        "幂等确认、维护影响分析，以及预防/告警工单、设备拓扑隔离、备件批次、"
        "持证技师排班、合并/分开停电方案校验与验收闸门。"
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(resources.router)
app.include_router(search.router)
app.include_router(reservations.router)
app.include_router(operations.router)
app.include_router(workorders.router)


@app.get("/health", tags=["health"])
def health():
    return {"status": "ok"}
