import pytest

from app.errors import DomainError
from app.modules.m6_batches import engine, versions
from app.store import connect, transaction


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    import app.store as store

    monkeypatch.setattr(store, "DATABASE_PATH", tmp_path / "db.sqlite3")
    yield


def _seed():
    return engine.open_batch(engine.SAMPLE)


def _ledger():
    with connect() as db:
        return {
            (row["doc_no"], row["line_no"]): dict(row)
            for row in db.execute("SELECT * FROM ledger_entries")
        }


def _registration(invoice_no):
    with connect() as db:
        row = db.execute("SELECT * FROM invoice_registrations WHERE invoice_no = ?", (invoice_no,)).fetchone()
        return dict(row) if row else None


def test_batch_binds_three_versions_and_commits():
    batch = _seed()
    assert batch["status"] == "committed"
    assert batch["versions"]["order"].startswith("order-v")
    assert batch["versions"]["acceptance"].startswith("acceptance-v")
    assert batch["versions"]["tax"].startswith("tax-v")

    heads = versions.state()["heads"]
    assert heads == batch["versions"]


def test_registered_evidence_survives_order_revision():
    first = _seed()
    registered = _registration("IV-001")
    assert registered["batch_id"] == first["batch_id"]
    ledger_before = _ledger()

    revised = engine.revise_order(
        "PO-001",
        [{"line_no": 1, "sku": "SKU-A", "qty": "12", "amount": "1200.00", "tax_rate": "13%"}],
    )
    assert revised["status"] == "committed"
    assert revised["versions"]["order"] != first["versions"]["order"]

    # The registration and its posted entries were not rewritten.
    assert _registration("IV-001") == registered
    assert _ledger() == ledger_before

    # Re-verification under the registration-time criteria still matches.
    check = engine.verify_registration("IV-001")
    assert check["status"] == "VERIFIED"
    assert check["versions"]["order"] == first["versions"]["order"]

    # The invoice is carried in the revision batch, not recomputed to 1200.
    carried = {item["item_key"]: item for item in revised["items"]}
    assert carried["carried:IV-001"]["status"] == "carried"
    assert carried["carried:IV-001"]["amount"] == "1130.00"


def test_new_invoice_conflict_is_quarantined():
    _seed()
    # Revise PO-003 down, so its acceptance (2) exceeds the new order (1).
    engine.revise_order(
        "PO-003",
        [{"line_no": 1, "sku": "SKU-C", "qty": "1", "amount": "100.00", "tax_rate": "13%"}],
    )
    # The new tax version adds a fresh invoice that would over-claim SKU-C.
    staged = versions.stage_version(
        "tax",
        engine.SAMPLE["tax"]
        + [{"doc_type": "IV", "doc_no": "IV-009", "line_no": 1, "sku": "SKU-C", "qty": "2", "amount": "226.00", "tax_rate": "13%"}],
    )
    batch = engine.open_batch({"tax_version": staged["version_id"]})
    assert batch["status"] == "committed"

    items = {item["item_key"]: item for item in batch["items"]}
    assert items["invoice:IV-009"]["status"] == "quarantined"
    assert items["invoice:IV-009"]["reason_code"] in {"QUANTITY_CONFLICT", "AMOUNT_CONFLICT"}
    # Earlier registrations still pass and are merely carried.
    assert items["carried:IV-001"]["status"] == "carried"
    assert _registration("IV-009") is None

    # No ledger entry was written for the quarantined invoice.
    assert not any(doc_no == "IV-009" for doc_no, _ in _ledger())


def test_failed_revision_keeps_old_head():
    first = _seed()
    with pytest.raises(DomainError) as error:
        engine.revise_order("PO-404", [{"sku": "SKU-A", "qty": "1", "amount": "1", "tax_rate": "13%"}])
    assert error.value.code == "ORDER_NOT_FOUND"

    heads = versions.state()["heads"]
    assert heads["order"] == first["versions"]["order"]
    listing = engine.list_batches()
    assert listing["current"] == first["batch_id"]
    assert listing["revisions"][0]["status"] == "rejected"
    assert listing["revisions"][0]["reason_code"] == "ORDER_NOT_FOUND"


def test_failed_revision_bad_line_keeps_old_batch():
    first = _seed()
    with pytest.raises(DomainError):
        engine.revise_order("PO-001", [{"line_no": 1, "sku": "SKU-A", "qty": "many", "amount": "1", "tax_rate": "13%"}])
    assert versions.state()["heads"]["order"] == first["versions"]["order"]
    with connect() as db:
        assert db.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1


def test_interrupted_batch_resumes_without_double_posting():
    _seed()
    staged = versions.stage_version(
        "tax",
        engine.SAMPLE["tax"]
        + [{"doc_type": "IV", "doc_no": "IV-010", "line_no": 1, "sku": "SKU-C", "qty": "2", "amount": "226.00", "tax_rate": "13%"}],
    )
    batch = engine.open_batch({"tax_version": staged["version_id"]}, process=False)
    assert batch["status"] == "open" and batch["checkpoint"] == 0

    before = _ledger()
    step = engine.process_batch(batch["batch_id"], max_items=1)
    assert step["status"] == "open" and step["checkpoint"] == 1
    step = engine.process_batch(batch["batch_id"], max_items=2)
    assert step["status"] == "open" and step["checkpoint"] == 3

    # Simulate a crash after item 3: item 3 is committed with checkpoint 3.
    final = engine.process_batch(batch["batch_id"])
    assert final["status"] == "committed"
    assert final["checkpoint"] == final["total_items"]

    after = _ledger()
    # Every pre-existing entry is untouched; the new invoice posted exactly once.
    for key, entry in before.items():
        assert after[key] == entry
    new_entries = [entry for key, entry in after.items() if key not in before]
    assert len(new_entries) == 1 and new_entries[0]["doc_no"] == "IV-010"
    assert new_entries[0]["batch_id"] == batch["batch_id"]

    # Retrying a committed batch is a no-op, not a second posting.
    again = engine.process_batch(batch["batch_id"])
    assert again["posted"] == final["posted"]
    assert _ledger() == after
    assert _registration("IV-010")["batch_id"] == batch["batch_id"]


def test_resume_after_item_written_before_checkpoint_does_not_duplicate():
    _seed()
    staged = versions.stage_version(
        "tax",
        engine.SAMPLE["tax"]
        + [{"doc_type": "IV", "doc_no": "IV-013", "line_no": 1, "sku": "SKU-C", "qty": "2", "amount": "226.00", "tax_rate": "13%"}],
    )
    batch = engine.open_batch({"tax_version": staged["version_id"]}, process=False)
    with connect() as db:
        plan = db.execute("SELECT seq, payload FROM batch_plan WHERE batch_id = ? AND seq = 3", (batch["batch_id"],)).fetchone()
        row = engine._batch_row(db, batch["batch_id"])
        engine._process_item(db, row, __import__("json").loads(plan["payload"]), 3)
    # Crash before the checkpoint moved: item 3 exists but checkpoint is 0.
    with connect() as db:
        assert db.execute("SELECT checkpoint FROM batches WHERE batch_id = ?", (batch["batch_id"],)).fetchone()[0] == 0
        posted_before = db.execute("SELECT COUNT(*) FROM ledger_entries WHERE doc_no = 'IV-013'").fetchone()[0]
    assert posted_before == 1
    final = engine.process_batch(batch["batch_id"])
    assert final["status"] == "committed"
    with connect() as db:
        posted_after = db.execute("SELECT COUNT(*) FROM ledger_entries WHERE doc_no = 'IV-013'").fetchone()[0]
        registrations = db.execute("SELECT COUNT(*) FROM invoice_registrations WHERE invoice_no = 'IV-013'").fetchone()[0]
    assert posted_after == 1 and registrations == 1


def test_retry_after_partial_write_does_not_duplicate():
    _seed()
    staged = versions.stage_version(
        "tax",
        engine.SAMPLE["tax"]
        + [
            {"doc_type": "IV", "doc_no": "IV-011", "line_no": 1, "sku": "SKU-C", "qty": "0", "amount": "0.00", "tax_rate": "13%"},
            {"doc_type": "IV", "doc_no": "IV-012", "line_no": 1, "sku": "SKU-C", "qty": "0", "amount": "0.00", "tax_rate": "13%"},
        ],
    )
    batch = engine.open_batch({"tax_version": staged["version_id"]})
    assert batch["status"] == "committed"
    first_count = len(_ledger())
    engine.process_batch(batch["batch_id"])
    assert len(_ledger()) == first_count


def test_export_only_selected_version():
    first = _seed()
    engine.revise_order(
        "PO-003",
        [{"line_no": 1, "sku": "SKU-C", "qty": "1", "amount": "100.00", "tax_rate": "13%"}],
    )
    current = engine.list_batches()["current"]
    assert current != first["batch_id"]

    old_body, old_id = engine.export_differences_csv(first["batch_id"])
    new_body, new_id = engine.export_differences_csv(current)
    default_body, default_id = engine.export_differences_csv()

    assert old_id == first["batch_id"] and new_id == current
    assert default_id == current
    assert default_body == new_body
    assert old_body != new_body
    assert "# batch_id=" + first["batch_id"] in old_body
    assert "# batch_id=" + current in new_body


def test_compare_reports_current_against_history():
    _seed()
    engine.revise_order(
        "PO-003",
        [{"line_no": 1, "sku": "SKU-C", "qty": "1", "amount": "100.00", "tax_rate": "13%"}],
    )
    listing = engine.list_batches()
    diff = listing["diff"]
    assert diff is not None
    assert diff["base"] != diff["target"]
    reasons = {row["reason_code"] for row in diff["differences"]["added"]}
    removed = {row["reason_code"] for row in diff["differences"]["removed"]}
    assert reasons == {"QUANTITY_CONFLICT"}
    assert removed == {"UNINVOICED"}


def test_version_content_is_immutable_and_reused():
    first = versions.stage_version("order", engine.SAMPLE["order"])
    second = versions.stage_version("order", list(reversed(engine.SAMPLE["order"])))
    assert first["version_id"] == second["version_id"]
    assert second["created"] is False

    with connect() as db:
        before = [tuple(row) for row in db.execute("SELECT * FROM dataset_lines WHERE version_id = ? ORDER BY doc_type, doc_no, line_no", (first["version_id"],))]
    with pytest.raises(RuntimeError):
        with transaction() as db:
            db.execute("UPDATE dataset_lines SET amount = '1.00' WHERE version_id = ?", (first["version_id"],))
            raise RuntimeError("abort edit")
    with connect() as db:
        after = [tuple(row) for row in db.execute("SELECT * FROM dataset_lines WHERE version_id = ? ORDER BY doc_type, doc_no, line_no", (first["version_id"],))]
    assert before == after


def test_http_flow_exposes_batches_and_selected_export():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    first = client.post("/api/reconcile", json={})
    assert first.status_code == 200
    batch = first.json()
    assert batch["status"] == "committed" and batch["current"] is True

    page = client.get("/batches")
    assert page.status_code == 200 and batch["batch_id"] in page.text
    assert "当前与历史差异" in page.text

    listing = client.get("/api/batches").json()
    assert listing["current"] == batch["batch_id"]
    assert client.get(f'/api/batches/{batch["batch_id"]}').json()["batch_id"] == batch["batch_id"]

    exported = client.get("/api/export/differences.csv", params={"batch_id": batch["batch_id"]})
    assert exported.status_code == 200
    assert exported.text.startswith(f"# batch_id={batch['batch_id']}")

    unknown = client.get("/api/reconcile")
    assert unknown.status_code == 405
    missing = client.post("/api/reconcile", json={"order_version": "order-vmissing"})
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "MISSING_VERSION"


def test_amended_invoice_in_new_version_does_not_rewrite_registration():
    _seed()
    registered = _registration("IV-002")

    amended_tax = [line for line in engine.SAMPLE["tax"] if line["doc_no"] != "IV-002"] + [
        {"doc_type": "IV", "doc_no": "IV-002", "line_no": 1, "sku": "SKU-B", "qty": "8", "amount": "900.00", "tax_rate": "9%"}
    ]
    staged = versions.stage_version("tax", amended_tax)
    batch = engine.open_batch({"tax_version": staged["version_id"]})

    items = {item["item_key"]: item for item in batch["items"]}
    assert items["carried:IV-002"]["status"] == "quarantined"
    assert items["carried:IV-002"]["reason_code"] == "EVIDENCE_DRIFT"
    # The original registration and ledger entry remain untouched.
    assert _registration("IV-002") == registered
    assert _ledger()[("IV-002", 1)]["amount"] == "872.00"
    check = engine.verify_registration("IV-002")
    assert check["status"] == "VERIFIED"
    assert check["amended_in_current_version"] is True


def test_historical_comparison_batch_does_not_become_current():
    first = _seed()
    revised = engine.revise_order(
        "PO-001",
        [{"line_no": 1, "sku": "SKU-A", "qty": "10", "amount": "1100.00", "tax_rate": "13%"}],
    )
    historical = engine.open_batch({
        "order_version": first["versions"]["order"],
        "acceptance_version": first["versions"]["acceptance"],
        "tax_version": first["versions"]["tax"],
    })
    assert historical["status"] == "committed"
    assert historical["current"] is False
    assert engine.list_batches()["current"] == revised["batch_id"]
    assert versions.state()["heads"]["order"] == revised["versions"]["order"]
