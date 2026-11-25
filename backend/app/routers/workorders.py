"""Feature-2 运营端接口：拓扑/技师/备件/工单、方案对比、排程、验收与回执。"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import IdempotentConflict, idempotent_endpoint
from ..models import Reservation, WorkOrder
from ..schemas import (
    AcceptanceRequest,
    DelayRequest,
    ReceiptCreate,
    ScheduleRequest,
    SignoffRequest,
    StartWorkRequest,
    TriageReadingRequest,
)
from ..services import workorders as svc

router = APIRouter(prefix="/api", tags=["work-orders"])


def _run_idempotent(db, request: Request, key: str, payload: dict, work):
    try:
        return idempotent_endpoint(db, key, request.url.path, payload, work)
    except IdempotentConflict as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.body)


# ---------- 只读资源 ----------

@router.get("/topology")
def topology(site_id: int = 1, db: Session = Depends(get_db)):
    return svc.topology_tree(db, site_id)


@router.get("/technicians")
def technicians(db: Session = Depends(get_db)):
    rows = db.scalars(select(svc.Technician).order_by(svc.Technician.id)).all()
    return [svc.technician_to_dict(t) for t in rows]


@router.get("/spare-parts")
def spare_parts(db: Session = Depends(get_db)):
    rows = db.scalars(select(svc.SparePart).order_by(svc.SparePart.id)).all()
    return [svc.part_to_dict(p) for p in rows]


@router.get("/work-orders")
def list_work_orders(db: Session = Depends(get_db)):
    rows = db.scalars(select(WorkOrder).order_by(WorkOrder.id)).all()
    return [svc.wo_to_dict(w) for w in rows]


@router.get("/work-orders/{wo_id}")
def get_work_order(wo_id: int, db: Session = Depends(get_db)):
    wo = db.get(WorkOrder, wo_id)
    if wo is None:
        raise HTTPException(404, "工单不存在")
    return svc.wo_to_dict(wo)


@router.get("/notifications")
def notifications(site_id: int = 1, db: Session = Depends(get_db)):
    return svc.notifications_out(db, site_id)


@router.get("/ops/board")
def ops_board(site_id: int = 1, db: Session = Depends(get_db)):
    return svc.board(db, site_id)


@router.get("/reservations/{reservation_id}/alternatives")
def reservation_alternatives(reservation_id: int, db: Session = Depends(get_db)):
    r = db.get(Reservation, reservation_id)
    if r is None:
        raise HTTPException(404, "预约不存在")
    return {
        "reservation_id": r.id,
        "ref": r.ref,
        "status": r.status.value,
        "connector_id": r.connector_id,
        # 所有建议 confirmed 恒为 False：只是候选，不会改写订单或锁定容量
        "suggestions": svc.suggest_for_reservation(db, r),
    }


# ---------- 方案对比（只读 dry-run，可反复调用） ----------

@router.post("/work-orders/compare")
def compare_plans(db: Session = Depends(get_db)):
    return svc.build_compare(db)


# ---------- 排程 / 开工 / 验收 / 签字 ----------

@router.post("/work-orders/schedule")
def schedule(req: ScheduleRequest, request: Request, db: Session = Depends(get_db)):
    payload = req.model_dump(mode="json")

    def work():
        return svc.apply_schedule(db, req)

    return _run_idempotent(db, request, req.idempotency_key, payload, work)


@router.post("/work-orders/triage-reading")
def triage(req: TriageReadingRequest, request: Request, db: Session = Depends(get_db)):
    payload = req.model_dump(mode="json")

    def work():
        return svc.triage_reading(db, req)

    return _run_idempotent(db, request, req.idempotency_key, payload, work)


@router.post("/work-orders/{wo_id}/start")
def start(wo_id: int, req: StartWorkRequest, request: Request,
          db: Session = Depends(get_db)):
    payload = {"wo_id": wo_id, **req.model_dump(mode="json")}

    def work():
        return svc.start_work(db, wo_id)

    return _run_idempotent(db, request, req.idempotency_key, payload, work)


@router.post("/work-orders/{wo_id}/delay")
def delay(wo_id: int, req: DelayRequest, request: Request,
          db: Session = Depends(get_db)):
    payload = {"wo_id": wo_id, **req.model_dump(mode="json")}

    def work():
        return svc.receive_delay(db, wo_id, req)

    return _run_idempotent(db, request, req.idempotency_key, payload, work)


@router.post("/work-orders/{wo_id}/acceptance")
def acceptance(wo_id: int, req: AcceptanceRequest, request: Request,
               db: Session = Depends(get_db)):
    payload = {"wo_id": wo_id, **req.model_dump(mode="json")}

    def work():
        return svc.record_acceptance(db, wo_id, req)

    return _run_idempotent(db, request, req.idempotency_key, payload, work)


@router.post("/work-orders/{wo_id}/signoff")
def signoff(wo_id: int, req: SignoffRequest, request: Request,
            db: Session = Depends(get_db)):
    payload = {"wo_id": wo_id, **req.model_dump(mode="json")}

    def work():
        return svc.signoff(db, wo_id, req)

    return _run_idempotent(db, request, req.idempotency_key, payload, work)


# ---------- 统一三类回执 ----------

@router.post("/receipts")
def receipts(req: ReceiptCreate, request: Request, db: Session = Depends(get_db)):
    payload = req.model_dump(mode="json")

    def work():
        return svc.receive_receipt(db, req)

    return _run_idempotent(db, request, req.idempotency_key, payload, work)
