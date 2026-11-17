from app.manifest import build_manifest
from app.store import record_run
from .compare import reconcile_amounts
from . import engine


def run(payload: bytes = b"procurematch-sample") -> dict:
    """Legacy one-click demo: record the content-addressed run and open the sample batch."""
    manifest = build_manifest(payload, 34, 2)
    manifest["created"] = record_run(manifest)
    batch = engine.sample_batch()
    account = reconcile_amounts("2260.00", "2034.00", "226.00", "0.00")
    return {**manifest, **account, "batch_id": batch["batch_id"], "versions": batch["versions"], "batch_status": batch["status"]}


def demo():
    return run()


def reconcile(payload: dict | None = None) -> dict:
    """Open a version-bound batch.

    An empty payload means the demo sample; otherwise the caller supplies
    either row data or explicit version ids for order / acceptance / tax.
    """
    if not payload:
        return engine.open_batch(engine.SAMPLE)
    return engine.open_batch(payload, process=bool(payload.get("process", True)))
