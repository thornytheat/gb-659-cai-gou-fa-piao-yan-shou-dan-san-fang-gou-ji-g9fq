from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from app.web import page
from .service import reconcile
from . import engine, versions

router = APIRouter()


def _batch_rows() -> list[dict]:
    listing = engine.list_batches()
    return [{
        "key": row["batch_id"],
        "status": "当前" if row["current"] else row["status"],
        "value": f'v{row["version_no"]} · {row["accepted"]}/{row["quarantined"]}/{row["carried"]}',
    } for row in listing["batches"]]


@router.get("/batches", response_class=HTMLResponse)
def view(request: Request):
    listing = engine.list_batches()
    return page(
        request, "m6.html", "差异批次", "/api/reconcile", _batch_rows(),
        context={"listing": listing, "diff": listing["diff"]},
    )


@router.post("/api/reconcile")
def execute(payload: dict | None = Body(default=None)):
    return reconcile(payload)


@router.post("/api/versions/{scope}")
def stage_version(scope: str, payload: dict = Body(...)):
    """Create an immutable version without moving the head.

    The head advances only when a batch bound to this version commits.
    """
    rows = payload.get("lines")
    if not isinstance(rows, list):
        from app.errors import DomainError
        raise DomainError("BAD_VERSION_INPUT", "缺少 lines 数组")
    return versions.stage_version(scope, rows)


@router.get("/api/versions")
def version_state():
    return versions.state()


@router.post("/api/batches")
def open_batch(payload: dict = Body(...)):
    return engine.open_batch(payload, process=bool(payload.get("process", True)))


@router.get("/api/batches")
def list_batches():
    return engine.list_batches()


@router.get("/api/batches/compare")
def compare_batches(base: str = Query(...), target: str = Query(...)):
    return engine.diff_batches(base, target)


@router.get("/api/batches/{batch_id}")
def get_batch(batch_id: str):
    return engine.get_batch(batch_id)


@router.post("/api/batches/{batch_id}/process")
def process_batch(batch_id: str, max_items: int | None = Query(default=None, ge=1)):
    return engine.process_batch(batch_id, max_items=max_items)


@router.post("/api/batches/{batch_id}/retry")
def retry_batch(batch_id: str):
    return engine.process_batch(batch_id)


@router.post("/api/orders/{order_no}/revisions")
def revise_order(order_no: str, payload: dict = Body(...)):
    return engine.revise_order(order_no, payload.get("lines", []))


@router.get("/api/invoices/{invoice_no}/registration")
def invoice_registration(invoice_no: str):
    return engine.verify_registration(invoice_no)


@router.get("/api/export/differences.csv")
def export_differences(batch_id: str | None = Query(default=None)):
    body, selected = engine.export_differences_csv(batch_id)
    filename = f"differences-{selected or 'none'}.csv"
    return PlainTextResponse(body, media_type="text/csv", headers={"content-disposition": f"attachment; filename={filename}"})
