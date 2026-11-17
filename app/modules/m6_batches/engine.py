"""Version-bound, resumable reconciliation batches.

Invariants
----------
* A batch never recomputes with facts other than the order / acceptance / tax
  version ids recorded on it at open time.
* Existing invoice registrations and ledger entries are append-only.  A new
  batch can observe a conflict, but it cannot rewrite a record that already
  passed.
* Every batch item is committed together with its checkpoint position, so an
  interrupted run resumes at the first unfinished item and posting is guarded
  by content-stable primary keys (retry never double-posts).
* The version heads and the current-batch pointer switch in one transaction,
  and only after every item has been written.  A rejected order revision
  leaves the old head, the old batch and all passed records untouched.
"""

import json
from decimal import Decimal
from hashlib import sha256

from app.config import RULE_VERSION
from app.errors import DomainError
from app.store import connect, now_iso, transaction
from app.modules.m4_tax.calculate import money
from . import versions
from .exporter import differences_csv as render_differences_csv

CENT = Decimal("0.01")
ZERO = Decimal("0")

# Demo evidence for the first download before any batch has been opened,
# mirroring the one-click sample's un-invoiced SKU-C acceptance.
DEMO_DIFFERENCE = {
    "business_key": "SKU-C|0.13",
    "line_no": 9,
    "reason_code": "UNINVOICED",
    "amount_cny": "226.00",
}


def _dec(value) -> Decimal:
    return Decimal(str(value))


def _num(value: Decimal) -> str:
    return versions.canonical_decimal(value)


def _canonical(value) -> str:
    return versions.canonical_json(value)


def _sha(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _bindings(row) -> dict:
    return {
        "order": row["order_version"],
        "acceptance": row["acceptance_version"],
        "tax": row["tax_version"],
    }


def _scope_key(line: dict) -> str:
    return f'{line["sku"]}|{line["tax_rate"]}'


# ---------------------------------------------------------------------------
# Three-way facts (pure calculation on immutable version lines)
# ---------------------------------------------------------------------------

def _group(lines_: list[dict]) -> dict[str, dict]:
    groups: dict[str, dict] = {}
    for line in lines_:
        key = _scope_key(line)
        group = groups.get(key)
        if group is None:
            group = {
                "business_key": key,
                "sku": line["sku"],
                "tax_rate": line["tax_rate"],
                "ordered_qty": ZERO,
                "order_net": ZERO,
                "received_qty": ZERO,
                "received_net": ZERO,
                "returned_qty": ZERO,
                "returned_net": ZERO,
            }
            groups[key] = group
        quantity, amount = _dec(line["qty"]), _dec(line["amount"])
        if line["doc_type"] == "PO":
            group["ordered_qty"] += quantity
            group["order_net"] += amount
        elif line["doc_type"] == "RT":
            group["returned_qty"] += quantity
            group["returned_net"] += amount
        else:
            group["received_qty"] += quantity
            group["received_net"] += amount
    return groups


def _blank_group(key: str, line: dict) -> dict:
    return {
        "business_key": key,
        "sku": line["sku"],
        "tax_rate": line["tax_rate"],
        "ordered_qty": ZERO,
        "order_net": ZERO,
        "received_qty": ZERO,
        "received_net": ZERO,
        "returned_qty": ZERO,
        "returned_net": ZERO,
    }


def _facts(db, bindings: dict) -> dict:
    order_lines = versions.lines(db, bindings["order"])
    acceptance_lines = versions.lines(db, bindings["acceptance"])
    tax_lines = versions.lines(db, bindings["tax"])
    groups = _group(order_lines + acceptance_lines)
    invoice_totals: dict[str, dict] = {}
    for line in tax_lines:
        key = _scope_key(line)
        if key not in groups:
            groups[key] = _blank_group(key, line)
        total = invoice_totals.setdefault(key, {"qty": ZERO, "gross": ZERO})
        total["qty"] += _dec(line["qty"])
        total["gross"] += _dec(line["amount"])
    derived = {}
    for key, group in groups.items():
        rate = _dec(group["tax_rate"])
        net_received = group["received_qty"] - group["returned_qty"]
        acceptance_net = group["received_net"] - group["returned_net"]
        acceptance_gross = money(acceptance_net * (1 + rate)) if acceptance_net else ZERO
        derived[key] = {
            **group,
            "net_received_qty": net_received,
            "acceptance_net": acceptance_net,
            "acceptance_gross": acceptance_gross,
            "open_qty": group["ordered_qty"] - net_received,
        }
    # Conflicts that exist on the ordered / accepted facts alone.
    fact_issues: dict[str, list[dict]] = {}
    for key, group in derived.items():
        issues = []
        if group["received_qty"] > 0 and group["ordered_qty"] == 0:
            issues.append(_issue("UNMATCHED_ORDER", ZERO, group["net_received_qty"], "group"))
        elif group["ordered_qty"] > 0 and group["net_received_qty"] > group["ordered_qty"]:
            issues.append(_issue("QUANTITY_CONFLICT", ZERO, group["net_received_qty"] - group["ordered_qty"], "group"))
        if issues:
            fact_issues[key] = issues
    return {
        "order_lines": order_lines,
        "acceptance_lines": acceptance_lines,
        "tax_lines": tax_lines,
        "groups": derived,
        "invoice_totals": invoice_totals,
        "fact_issues": fact_issues,
    }


def _issue(reason: str, amount: Decimal, qty: Decimal, level: str) -> dict:
    return {"reason": reason, "amount": amount, "qty": qty, "level": level}


def _expected_gross(group: dict, invoice_qty: Decimal) -> Decimal | None:
    if group["ordered_qty"] > 0:
        unit_net = group["order_net"] / group["ordered_qty"]
    elif group["net_received_qty"] > 0:
        unit_net = group["acceptance_net"] / group["net_received_qty"]
    else:
        return None
    return money(invoice_qty * unit_net * (1 + _dec(group["tax_rate"])))


ISSUE_PRIORITY = (
    "UNKNOWN_REFERENCE",
    "MISSING_ACCEPTANCE",
    "UNMATCHED_ORDER",
    "EVIDENCE_DRIFT",
    "QUANTITY_CONFLICT",
    "AMOUNT_CONFLICT",
    "TAX_DIFFERENCE",
)


def _primary(reasons) -> str:
    reasons = set(reasons)
    for reason in ISSUE_PRIORITY:
        if reason in reasons:
            return reason
    return sorted(reasons)[0]


# ---------------------------------------------------------------------------
# Evidence pinned at registration
# ---------------------------------------------------------------------------

def _invoice_content(db, invoice_no: str, tax_version: str) -> dict:
    lines_ = [line for line in versions.lines(db, tax_version) if line["doc_no"] == invoice_no]
    return {
        "invoice_no": invoice_no,
        "lines": [
            {"line_no": line["line_no"], "sku": line["sku"], "tax_rate": line["tax_rate"], "qty": line["qty"], "amount": line["amount"]}
            for line in lines_
        ],
    }


def _invoice_evidence(db, invoice_no: str, bindings: dict, facts: dict | None = None) -> dict:
    facts = facts if facts is not None else _facts(db, bindings)
    content = _invoice_content(db, invoice_no, bindings["tax"])
    keys = sorted({_scope_key(line) for line in content["lines"]})
    group_view = {}
    for key in keys:
        group = facts["groups"][key]
        invoice = facts["invoice_totals"].get(key, {"qty": ZERO, "gross": ZERO})
        group_view[key] = {
            "ordered_qty": _num(group["ordered_qty"]),
            "order_net": str(money(group["order_net"])),
            "net_received_qty": _num(group["net_received_qty"]),
            "acceptance_gross": str(money(group["acceptance_gross"])),
            "open_qty": _num(group["open_qty"]),
            "invoice_qty": _num(invoice["qty"]),
            "invoice_gross": str(money(invoice["gross"])),
            "uninvoiced": str(money(group["acceptance_gross"] - invoice["gross"])),
            "conflicts": sorted({issue["reason"] for issue in facts["fact_issues"].get(key, [])}),
        }
    pinned = {}
    for scope, version_id in bindings.items():
        version = versions.get_version(db, version_id)
        pinned[scope] = {
            "version_id": version_id,
            "version_no": version["version_no"],
            "input_sha256": version["input_sha256"],
        }
    return {
        "invoice_no": invoice_no,
        "rule_version": RULE_VERSION,
        "versions": pinned,
        "lines": content["lines"],
        "groups": group_view,
    }


def _evidence_sha(evidence: dict) -> str:
    return _sha(_canonical(evidence))


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

def _plan(db, bindings: dict) -> list[dict]:
    facts = _facts(db, bindings)
    invoice_docs: dict[str, set[str]] = {}
    for line in facts["tax_lines"]:
        invoice_docs.setdefault(line["doc_no"], set()).add(_scope_key(line))
    registered = {row["invoice_no"] for row in db.execute("SELECT invoice_no FROM invoice_registrations")}
    plan = []
    for invoice_no in sorted(invoice_docs):
        kind = "carried" if invoice_no in registered else "invoice"
        plan.append({"kind": kind, "key": f"{kind}:{invoice_no}", "invoice_no": invoice_no, "business_keys": sorted(invoice_docs[invoice_no])})
    # Every previously registered invoice is re-verified with its own pinned
    # criteria, even if the current tax version no longer lists it.
    for invoice_no in sorted(registered - set(invoice_docs)):
        plan.append({"kind": "carried", "key": f"carried:{invoice_no}", "invoice_no": invoice_no, "business_keys": []})
    for key in sorted(facts["fact_issues"]):
        plan.append({"kind": "conflict", "key": f"conflict:{key}", "business_key": key})
    for key in sorted(facts["groups"]):
        group = facts["groups"][key]
        if key in facts["fact_issues"]:
            continue
        if group["acceptance_gross"] > ZERO or group["ordered_qty"] > ZERO:
            plan.append({"kind": "residual", "key": f"residual:{key}", "business_key": key})
    return plan


def _posted_totals(db) -> dict[str, Decimal]:
    """Invoice gross actually registered and posted, per business key.

    Residuals must reconcile against the immutable ledger, not against a
    current tax version that may list quarantined or amended invoices.
    """
    totals: dict[str, Decimal] = {}
    for row in db.execute("SELECT business_key, amount FROM ledger_entries WHERE doc_type = 'IV'"):
        totals[row["business_key"]] = totals.get(row["business_key"], ZERO) + _dec(row["amount"])
    return totals


# ---------------------------------------------------------------------------
# Batch lifecycle
# ---------------------------------------------------------------------------

def _create_batch(db, bindings: dict) -> tuple[dict, bool]:
    identity = _sha(_canonical({"rule_version": RULE_VERSION, "versions": bindings}))
    batch_id = f"batch-{identity[:12]}"
    existing = db.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
    if existing:
        return dict(existing), False
    plan = _plan(db, bindings)
    version_no = db.execute("SELECT COALESCE(MAX(version_no), 0) + 1 AS next FROM batches").fetchone()["next"]
    db.execute(
        "INSERT INTO batches (batch_id, version_no, input_sha256, input_json, rule_version, order_version, acceptance_version, tax_version, "
        "status, checkpoint, total_items, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', 0, ?, ?)",
        (batch_id, version_no, identity, _canonical({"versions": bindings}), RULE_VERSION,
         bindings["order"], bindings["acceptance"], bindings["tax"], len(plan), now_iso()),
    )
    for seq, item in enumerate(plan, start=1):
        db.execute(
            "INSERT INTO batch_plan (batch_id, seq, item_key, payload) VALUES (?, ?, ?, ?)",
            (batch_id, seq, item["key"], _canonical(item)),
        )
    row = db.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
    return dict(row), True


def _resolve(db, payload: dict) -> dict:
    bindings = {}
    for scope in versions.SCOPES:
        rows = payload.get(scope)
        explicit = payload.get(f"{scope}_version")
        if rows is not None and explicit:
            raise DomainError("VERSION_CONFLICT", f"{scope} 不能同时提供行数据和版本号")
        if rows is not None:
            bindings[scope] = versions.create_version(db, scope, rows)["version_id"]
        elif explicit:
            record = versions.get_version(db, explicit)
            if record is None:
                raise DomainError("MISSING_VERSION", f"数据版本不存在: {explicit}", 404)
            if record["scope"] != scope:
                raise DomainError("VERSION_SCOPE_MISMATCH", f"版本 {explicit} 不属于 {scope}")
            bindings[scope] = explicit
        else:
            current = versions.head(db, scope)
            if current is None:
                raise DomainError("MISSING_VERSION", f"缺少 {scope} 的当前版本", 409)
            bindings[scope] = current
    return bindings


def _batch_row(db, batch_id: str):
    row = db.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
    if row is None:
        raise DomainError("MISSING_BATCH", f"批次不存在: {batch_id}", 404)
    return row


def open_batch(payload: dict | None = None, process: bool = True) -> dict:
    payload = payload or {}
    with transaction() as db:
        bindings = _resolve(db, payload)
        batch, created = _create_batch(db, bindings)
    if process:
        return process_batch(batch["batch_id"])
    return _summary(batch["batch_id"])


def process_batch(batch_id: str, max_items: int | None = None) -> dict:
    processed = 0
    while True:
        with transaction() as db:
            batch = _batch_row(db, batch_id)
            if batch["status"] == "committed":
                return _summary(batch_id)
            if batch["checkpoint"] >= batch["total_items"]:
                _finalize(db, batch_id)
                continue
            if max_items is not None and processed >= max_items:
                return _summary(batch_id)
            seq = batch["checkpoint"] + 1
            plan = db.execute("SELECT payload FROM batch_plan WHERE batch_id = ? AND seq = ?", (batch_id, seq)).fetchone()
            if plan is None:
                raise DomainError("MISSING_PLAN", f"批次 {batch_id} 缺少第 {seq} 项计划")
            already = db.execute("SELECT 1 FROM batch_items WHERE batch_id = ? AND seq = ?", (batch_id, seq)).fetchone()
            if already is None:
                _process_item(db, batch, json.loads(plan["payload"]), seq)
            db.execute("UPDATE batches SET checkpoint = ? WHERE batch_id = ?", (seq, batch_id))
            processed += 1


def _process_item(db, batch, item: dict, seq: int) -> None:
    bindings = _bindings(batch)
    facts = _facts(db, bindings)
    if item["kind"] in ("invoice", "carried"):
        _process_invoice(db, batch, bindings, facts, item, seq)
    elif item["kind"] == "conflict":
        _process_conflict(db, batch, facts, item, seq)
    else:
        _process_residual(db, batch, facts, item, seq)


def _write_item(db, batch, seq, item, status, reason, amount, qty, posted=0, evidence=None):
    db.execute(
        "INSERT OR IGNORE INTO batch_items (batch_id, seq, item_key, doc_type, doc_no, line_no, business_key, status, reason_code, amount, qty, posted, evidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (batch["batch_id"], seq, item["key"],
         "IV" if item.get("invoice_no") else "GROUP",
         item.get("invoice_no", ""), item.get("line_no", 0),
         item.get("business_key", ",".join(item.get("business_keys", []))),
         status, reason, str(money(amount)), _num(qty), posted,
         _canonical(evidence) if evidence is not None else None),
    )


def _write_difference(db, batch, business_key, line_no, reason, amount, qty=ZERO):
    db.execute(
        "INSERT OR IGNORE INTO batch_differences (batch_id, business_key, line_no, reason_code, amount_cny, qty) VALUES (?, ?, ?, ?, ?, ?)",
        (batch["batch_id"], business_key, line_no, reason, str(money(amount)), _num(qty)),
    )


def _registered_elsewhere(db, exclude: str) -> dict[str, dict]:
    """Capacity already claimed by invoices registered before this item.

    Posted ledger entries are the source of truth: they persist even when a
    later tax version no longer lists the invoice, and entries written by
    earlier items of this same batch count too.  Two new invoices for one
    receipt group therefore cannot both consume the full quantity.
    """
    reserved: dict[str, dict] = {}
    for row in db.execute("SELECT doc_no, business_key, amount, qty FROM ledger_entries WHERE doc_type = 'IV'"):
        if row["doc_no"] == exclude:
            continue
        bucket = reserved.setdefault(row["business_key"], {"qty": ZERO, "gross": ZERO})
        bucket["qty"] += _dec(row["qty"])
        bucket["gross"] += _dec(row["amount"])
    return reserved


def _invoice_issues(db, facts, invoice_no: str) -> tuple[list[dict], list[dict]]:
    """Return (fact issues, invoice issues) for one new invoice.

    ``fact issues`` belong to the order/acceptance pair itself.  ``invoice
    issues`` compare this invoice against the remaining capacity after
    invoices already posted to the immutable ledger.
    """
    keys = sorted({_scope_key(line) for line in facts["tax_lines"] if line["doc_no"] == invoice_no})
    lines_ = [line for line in facts["tax_lines"] if line["doc_no"] == invoice_no]
    gross = sum((_dec(line["amount"]) for line in lines_), ZERO)
    qty = sum((_dec(line["qty"]) for line in lines_), ZERO)
    fact_issues, invoice_issues = [], []
    for key in keys:
        for issue in facts["fact_issues"].get(key, []):
            fact_issues.append({**issue, "business_key": key})
    if any(issue["reason"] == "UNMATCHED_ORDER" for issue in fact_issues):
        return _dedupe(fact_issues), []
    reserved = _registered_elsewhere(db, invoice_no)
    for key in keys:
        group = facts["groups"][key]
        line_no = next(line["line_no"] for line in lines_ if _scope_key(line) == key)
        if group["received_qty"] == 0 and group["ordered_qty"] == 0:
            invoice_issues.append({**_issue("UNKNOWN_REFERENCE", gross, qty, "invoice"), "business_key": key, "line_no": line_no})
            continue
        if group["received_qty"] == 0:
            invoice_issues.append({**_issue("MISSING_ACCEPTANCE", gross, qty, "invoice"), "business_key": key, "line_no": line_no})
            continue
        bucket = reserved.get(key, {"qty": ZERO, "gross": ZERO})
        available_qty = group["net_received_qty"] - bucket["qty"]
        available_gross = group["acceptance_gross"] - bucket["gross"]
        own_qty = sum((_dec(line["qty"]) for line in lines_ if _scope_key(line) == key), ZERO)
        own_gross = sum((_dec(line["amount"]) for line in lines_ if _scope_key(line) == key), ZERO)
        if own_qty > available_qty:
            invoice_issues.append({**_issue("QUANTITY_CONFLICT", ZERO, own_qty - available_qty, "invoice"), "business_key": key, "line_no": line_no})
        if own_gross > available_gross + CENT:
            invoice_issues.append({**_issue("AMOUNT_CONFLICT", own_gross - available_gross, ZERO, "invoice"), "business_key": key, "line_no": line_no})
        expected = _expected_gross(group, own_qty)
        if expected is not None and abs(own_gross - expected) > CENT:
            invoice_issues.append({**_issue("TAX_DIFFERENCE", own_gross - expected, ZERO, "invoice"), "business_key": key, "line_no": line_no})
    return _dedupe(fact_issues), _dedupe(invoice_issues)


def _dedupe(issues: list[dict]) -> list[dict]:
    """Keep one issue per (business key, reason), preserving order."""
    seen, result = set(), []
    for issue in issues:
        token = (issue.get("business_key"), issue["reason"])
        if token not in seen:
            seen.add(token)
            result.append(issue)
    return result


def _process_invoice(db, batch, bindings, facts, item, seq):
    invoice_no = item["invoice_no"]
    registered = db.execute("SELECT * FROM invoice_registrations WHERE invoice_no = ?", (invoice_no,)).fetchone()

    if registered is not None:
        # Already registered — normally planned as "carried"; the explicit
        # re-check also covers a concurrent batch registering it first.
        _process_carried(db, batch, bindings, invoice_no, registered, item, seq)
        return

    lines_ = [line for line in facts["tax_lines"] if line["doc_no"] == invoice_no]
    gross = sum((_dec(line["amount"]) for line in lines_), ZERO)
    qty = sum((_dec(line["qty"]) for line in lines_), ZERO)
    first_line_no = lines_[0]["line_no"] if lines_ else 0
    fact_issues, invoice_issues = _invoice_issues(db, facts, invoice_no)
    all_issues = fact_issues + invoice_issues

    if all_issues:
        reason = _primary(issue["reason"] for issue in all_issues)
        _write_item(db, batch, seq, {**item, "line_no": first_line_no}, "quarantined", reason, gross, qty)
        for issue in all_issues:
            _write_difference(db, batch, issue["business_key"], issue.get("line_no", 0), issue["reason"], issue["amount"], issue["qty"])
        return

    evidence = _invoice_evidence(db, invoice_no, bindings, facts)
    content_sha = _sha(_canonical(_invoice_content(db, invoice_no, bindings["tax"])))
    db.execute(
        "INSERT INTO invoice_registrations (invoice_no, content_sha256, rule_version, order_version, acceptance_version, tax_version, business_key, status, evidence, evidence_sha256, batch_id, registered_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?, ?, ?)",
        (invoice_no, content_sha, RULE_VERSION, bindings["order"], bindings["acceptance"], bindings["tax"],
         ",".join(item["business_keys"]), _canonical(evidence), _evidence_sha(evidence), batch["batch_id"], now_iso()),
    )
    posted = 0
    for line in lines_:
        before = db.total_changes
        db.execute(
            "INSERT OR IGNORE INTO ledger_entries (doc_type, doc_no, line_no, business_key, amount, qty, batch_id, order_version, acceptance_version, tax_version, posted_at) "
            "VALUES ('IV', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (line["doc_no"], line["line_no"], _scope_key(line), line["amount"], line["qty"], batch["batch_id"],
             bindings["order"], bindings["acceptance"], bindings["tax"], now_iso()),
        )
        posted += db.total_changes - before
    _write_item(db, batch, seq, {**item, "line_no": first_line_no}, "accepted", None, gross, qty, posted=posted, evidence=evidence)


def _process_carried(db, batch, bindings, invoice_no, registered, item, seq):
    """Re-verify a registered invoice with its registration-time three-way criteria.

    The registration and its ledger entries are never rewritten.  Two things
    can put the item into quarantine with evidence: the pinned evidence no
    longer recomputes from the registration-time versions, or the current tax
    version lists the same invoice number with different content (an amended
    invoice).  In both cases the passed record itself stays as it was.
    """
    pinned = {
        "order": registered["order_version"],
        "acceptance": registered["acceptance_version"],
        "tax": registered["tax_version"],
    }
    registered_content = _invoice_content(db, invoice_no, registered["tax_version"])
    current_content = _invoice_content(db, invoice_no, bindings["tax"])
    gross = sum((_dec(line["amount"]) for line in registered_content["lines"]), ZERO)
    qty = sum((_dec(line["qty"]) for line in registered_content["lines"]), ZERO)
    first_line_no = registered_content["lines"][0]["line_no"] if registered_content["lines"] else 0
    evidence = _invoice_evidence(db, invoice_no, pinned)
    drift = registered["evidence_sha256"] != _evidence_sha(evidence)
    if current_content["lines"] and _sha(_canonical(current_content)) != registered["content_sha256"]:
        drift = True
    if drift:
        _write_item(db, batch, seq, {**item, "line_no": first_line_no}, "quarantined", "EVIDENCE_DRIFT", gross, qty, evidence=evidence)
        key = registered["business_key"].split(",")[0]
        _write_difference(db, batch, key, first_line_no, "EVIDENCE_DRIFT", gross, qty)
        return
    # A passed registration is re-verified only under its own pinned criteria.
    # Current-version conflicts are reported on new invoices, never by
    # rewriting evidence that already passed.
    _write_item(db, batch, seq, {**item, "line_no": first_line_no}, "carried", None, gross, qty, evidence=evidence)


def _process_conflict(db, batch, facts, item, seq):
    key = item["business_key"]
    group = facts["groups"][key]
    issues = facts["fact_issues"][key]
    reason = _primary(issue["reason"] for issue in issues)
    _write_item(db, batch, seq, {**item, "business_key": key}, "quarantined", reason, group["acceptance_gross"], group["net_received_qty"])
    for issue in issues:
        _write_difference(db, batch, key, 0, issue["reason"], issue["amount"], issue["qty"])


def _process_residual(db, batch, facts, item, seq):
    key = item["business_key"]
    group = facts["groups"][key]
    invoiced = _posted_totals(db).get(key, ZERO)
    uninvoiced = money(group["acceptance_gross"] - invoiced)
    _write_item(db, batch, seq, {**item, "business_key": key}, "accepted", None, uninvoiced, ZERO)
    if uninvoiced > ZERO:
        _write_difference(db, batch, key, 0, "UNINVOICED", uninvoiced)
    if group["open_qty"] != ZERO:
        _write_difference(db, batch, key, 0, "OPEN_QUANTITY", ZERO, group["open_qty"])


def _finalize(db, batch_id: str):
    batch = _batch_row(db, batch_id)
    counts = db.execute(
        "SELECT "
        "COALESCE(SUM(status = 'accepted'), 0) AS accepted, "
        "COALESCE(SUM(status = 'quarantined'), 0) AS quarantined, "
        "COALESCE(SUM(status = 'carried'), 0) AS carried "
        "FROM batch_items WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    posted = db.execute("SELECT COUNT(*) FROM ledger_entries WHERE batch_id = ?", (batch_id,)).fetchone()[0]
    db.execute(
        "UPDATE batches SET status = 'committed', checkpoint = total_items, accepted = ?, quarantined = ?, carried = ?, posted = ?, committed_at = ? "
        "WHERE batch_id = ?",
        (counts["accepted"], counts["quarantined"], counts["carried"], posted, now_iso(), batch_id),
    )
    # Only a committed batch may move the version heads; unreferenced older
    # batches never roll a newer head back.
    for scope, version_id in _bindings(batch).items():
        version = versions.get_version(db, version_id)
        current = versions.head(db, scope)
        if current is None:
            db.execute("INSERT INTO dataset_heads (scope, version_id, updated_at) VALUES (?, ?, ?)", (scope, version_id, now_iso()))
            continue
        current_version = versions.get_version(db, current)
        if current_version is None or version["version_no"] > current_version["version_no"]:
            db.execute("UPDATE dataset_heads SET version_id = ?, updated_at = ? WHERE scope = ?", (version_id, now_iso(), scope))
    # A batch opened deliberately against historical versions is a comparison
    # run, not the new state of the world: it never becomes "current".
    behind = False
    for scope, version_id in _bindings(batch).items():
        head_version_id = versions.head(db, scope)
        if head_version_id is None:
            continue
        head_version = versions.get_version(db, head_version_id)
        version = versions.get_version(db, version_id)
        if head_version is not None and version["version_no"] < head_version["version_no"]:
            behind = True
            break
    if behind:
        return
    current = db.execute("SELECT batch_id FROM batch_current WHERE scope = 'purchase'").fetchone()
    if current is None:
        db.execute("INSERT INTO batch_current (scope, batch_id) VALUES ('purchase', ?)", (batch_id,))
    else:
        current_batch = _batch_row(db, current["batch_id"])
        if batch["version_no"] >= current_batch["version_no"]:
            db.execute("UPDATE batch_current SET batch_id = ? WHERE scope = 'purchase'", (batch_id,))


# ---------------------------------------------------------------------------
# Order revision: validate -> new version + new batch -> process -> atomic switch
# ---------------------------------------------------------------------------

def revise_order(order_no: str, lines_: list[dict]) -> dict:
    order_no = str(order_no).strip()
    if not order_no:
        raise DomainError("EMPTY_VALUE", "订单号不能为空")
    if not isinstance(lines_, list) or not lines_:
        raise DomainError("EMPTY_VALUE", "修订必须至少保留一行订单行")
    try:
        with transaction() as db:
            current = versions.head(db, "order")
            if current is None:
                raise DomainError("MISSING_VERSION", "缺少订单当前版本", 409)
            existing = versions.lines(db, current)
            if not any(line["doc_no"] == order_no for line in existing):
                raise DomainError("ORDER_NOT_FOUND", f"订单不存在: {order_no}", 404)
            replacement = []
            for row in lines_:
                if not isinstance(row, dict):
                    raise DomainError("BAD_VERSION_INPUT", "订单行必须是对象")
                if str(row.get("doc_no", order_no)).strip() != order_no:
                    raise DomainError("DOC_NO_MISMATCH", f"修订行订单号必须为 {order_no}")
                replacement.append({**row, "doc_type": "PO", "doc_no": order_no})
            merged = [line for line in existing if line["doc_no"] != order_no] + replacement
            version = versions.create_version(db, "order", merged)
            acceptance, tax = versions.head(db, "acceptance"), versions.head(db, "tax")
            if acceptance is None or tax is None:
                raise DomainError("MISSING_VERSION", "缺少验收或税额当前版本", 409)
            bindings = {"order": version["version_id"], "acceptance": acceptance, "tax": tax}
            batch, created = _create_batch(db, bindings)
            revision_id = f"rev-{_sha(_canonical({'order_no': order_no, 'batch': batch['batch_id'], 'to': version['version_id']}))[:12]}"
            db.execute(
                "INSERT OR IGNORE INTO order_revisions (revision_id, order_no, from_version, to_version, batch_id, status, reason_code, message, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', NULL, NULL, ?)",
                (revision_id, order_no, current, version["version_id"], batch["batch_id"], now_iso()),
            )
    except DomainError as error:
        _log_rejection(order_no, error)
        raise
    result = process_batch(batch["batch_id"])
    with transaction() as db:
        status = "committed" if result["status"] == "committed" else "open"
        db.execute("UPDATE order_revisions SET status = ? WHERE revision_id = ?", (status, revision_id))
    return {
        **result,
        "revision": {
            "revision_id": revision_id,
            "order_no": order_no,
            "from_version": current,
            "to_version": version["version_id"],
            "created": created,
        },
    }


def _log_rejection(order_no: str, error: DomainError):
    with transaction() as db:
        current = versions.head(db, "order")
        revision_id = f"rev-{_sha(_canonical({'order_no': order_no, 'from': current, 'reason': error.code, 'at': now_iso()}))[:12]}"
        db.execute(
            "INSERT OR IGNORE INTO order_revisions (revision_id, order_no, from_version, to_version, batch_id, status, reason_code, message, created_at) "
            "VALUES (?, ?, ?, NULL, NULL, 'rejected', ?, ?, ?)",
            (revision_id, order_no, current, error.code, error.message, now_iso()),
        )


# ---------------------------------------------------------------------------
# Queries / comparison / export
# ---------------------------------------------------------------------------

def _summary(batch_id: str) -> dict:
    with connect() as db:
        batch = _batch_row(db, batch_id)
        current = db.execute("SELECT batch_id FROM batch_current WHERE scope = 'purchase'").fetchone()
        items = [dict(row) for row in db.execute("SELECT * FROM batch_items WHERE batch_id = ? ORDER BY seq", (batch_id,))]
        differences = [dict(row) for row in db.execute(
            "SELECT business_key, line_no, reason_code, amount_cny, qty FROM batch_differences WHERE batch_id = ? ORDER BY business_key, line_no, reason_code",
            (batch_id,),
        )]
    return {
        "batch_id": batch["batch_id"],
        "version_no": batch["version_no"],
        "status": batch["status"],
        "current": bool(current and current["batch_id"] == batch_id),
        "checkpoint": batch["checkpoint"],
        "total_items": batch["total_items"],
        "accepted": batch["accepted"],
        "quarantined": batch["quarantined"],
        "carried": batch["carried"],
        "posted": batch["posted"],
        "input_sha256": batch["input_sha256"],
        "rule_version": batch["rule_version"],
        "versions": _bindings(batch),
        "created_at": batch["created_at"],
        "committed_at": batch["committed_at"],
        "items": items,
        "differences": differences,
    }


def get_batch(batch_id: str) -> dict:
    return _summary(batch_id)


def list_batches() -> dict:
    with connect() as db:
        current = db.execute("SELECT batch_id FROM batch_current WHERE scope = 'purchase'").fetchone()
        rows = [dict(row) for row in db.execute("SELECT * FROM batches ORDER BY version_no DESC")]
        revisions = [dict(row) for row in db.execute("SELECT * FROM order_revisions ORDER BY created_at DESC, revision_id DESC LIMIT 20")]
    current_id = current["batch_id"] if current else None
    batches = [{
        "batch_id": row["batch_id"],
        "version_no": row["version_no"],
        "status": row["status"],
        "current": row["batch_id"] == current_id,
        "checkpoint": row["checkpoint"],
        "total_items": row["total_items"],
        "accepted": row["accepted"],
        "quarantined": row["quarantined"],
        "carried": row["carried"],
        "posted": row["posted"],
        "versions": {"order": row["order_version"], "acceptance": row["acceptance_version"], "tax": row["tax_version"]},
        "created_at": row["created_at"],
        "committed_at": row["committed_at"],
    } for row in rows]
    previous_id = None
    for batch in batches:
        if batch["status"] == "committed" and batch["batch_id"] != current_id:
            previous_id = batch["batch_id"]
            break
    diff = diff_batches(previous_id, current_id) if previous_id and current_id else None
    return {"current": current_id, "batches": batches, "revisions": revisions, "diff": diff}


def diff_batches(base_id: str | None, target_id: str | None) -> dict:
    if not base_id or not target_id:
        raise DomainError("MISSING_BATCH", "比较批次需要 base 与 target", 422)
    with connect() as db:
        _batch_row(db, base_id)
        _batch_row(db, target_id)

        def load(table, key_fields, extra):
            target = {tuple(row[field] for field in key_fields): dict(row) for row in db.execute(f"SELECT * FROM {table} WHERE batch_id = ?", (target_id,))}
            base = {tuple(row[field] for field in key_fields): dict(row) for row in db.execute(f"SELECT * FROM {table} WHERE batch_id = ?", (base_id,))}
            added = [target[key] for key in sorted(target.keys() - base.keys(), key=str)]
            removed = [base[key] for key in sorted(base.keys() - target.keys(), key=str)]
            changed = []
            for key in sorted(base.keys() & target.keys(), key=str):
                fields = {field: {"from": base[key][field], "to": target[key][field]} for field in extra if base[key][field] != target[key][field]}
                if fields:
                    changed.append({"key": list(key), "fields": fields})
            return added, removed, changed

        diff_added, diff_removed, diff_changed = load(
            "batch_differences", ("business_key", "line_no", "reason_code"), ("amount_cny", "qty"))
        item_added, item_removed, item_changed = load(
            "batch_items", ("item_key",), ("status", "reason_code", "amount", "qty"))
    return {
        "base": base_id,
        "target": target_id,
        "differences": {"added": diff_added, "removed": diff_removed, "changed": diff_changed},
        "items": {"added": item_added, "removed": item_removed, "changed": item_changed},
        "counts": {
            "differences_added": len(diff_added),
            "differences_removed": len(diff_removed),
            "differences_changed": len(diff_changed),
            "items_added": len(item_added),
            "items_removed": len(item_removed),
            "items_changed": len(item_changed),
        },
    }


def export_rows(batch_id: str | None = None) -> tuple[list[dict], str]:
    with connect() as db:
        if batch_id:
            _batch_row(db, batch_id)
        else:
            current = db.execute("SELECT batch_id FROM batch_current WHERE scope = 'purchase'").fetchone()
            if current is None:
                # Nothing has been reconciled yet: fall back to the demo sample
                # evidence so the download still shows the evidence columns.
                has_batch = db.execute("SELECT 1 FROM batches LIMIT 1").fetchone()
                return ([] if has_batch else [dict(DEMO_DIFFERENCE)]), ""
            batch_id = current["batch_id"]
        rows = [dict(row) for row in db.execute(
            "SELECT business_key, line_no, reason_code, amount_cny FROM batch_differences WHERE batch_id = ? ORDER BY business_key, line_no, reason_code",
            (batch_id,),
        )]
    return rows, batch_id


def export_differences_csv(batch_id: str | None = None) -> tuple[str, str]:
    rows, selected = export_rows(batch_id)
    header = f"# batch_id={selected or 'none'}\n" if selected else ""
    return header + render_differences_csv(rows), selected


def verify_registration(invoice_no: str) -> dict:
    with connect() as db:
        registered = db.execute("SELECT * FROM invoice_registrations WHERE invoice_no = ?", (invoice_no,)).fetchone()
        if registered is None:
            raise DomainError("MISSING_REGISTRATION", f"发票未登记: {invoice_no}", 404)
        bindings = {
            "order": registered["order_version"],
            "acceptance": registered["acceptance_version"],
            "tax": registered["tax_version"],
        }
        evidence = _invoice_evidence(db, invoice_no, bindings)
        content = _invoice_content(db, invoice_no, registered["tax_version"])
        current_tax = versions.head(db, "tax")
        current_content = _invoice_content(db, invoice_no, current_tax) if current_tax else {"lines": []}
    current_sha = _evidence_sha(evidence)
    return {
        "invoice_no": invoice_no,
        "batch_id": registered["batch_id"],
        "registered_at": registered["registered_at"],
        "versions": bindings,
        "evidence_sha256": registered["evidence_sha256"],
        "recomputed_sha256": current_sha,
        "content_match": registered["content_sha256"] == _sha(_canonical(content)),
        "current_tax_version": current_tax,
        "amended_in_current_version": bool(current_content["lines"]) and _sha(_canonical(current_content)) != registered["content_sha256"],
        "status": "VERIFIED" if current_sha == registered["evidence_sha256"] else "EVIDENCE_DRIFT",
        "evidence": evidence,
    }


# ---------------------------------------------------------------------------
# Sample used by the one-click demo
# ---------------------------------------------------------------------------

SAMPLE = {
    "order": [
        {"doc_type": "PO", "doc_no": "PO-001", "line_no": 1, "sku": "SKU-A", "qty": "10", "amount": "1000.00", "tax_rate": "13%"},
        {"doc_type": "PO", "doc_no": "PO-002", "line_no": 1, "sku": "SKU-B", "qty": "8", "amount": "800.00", "tax_rate": "9%"},
        {"doc_type": "PO", "doc_no": "PO-003", "line_no": 1, "sku": "SKU-C", "qty": "2", "amount": "200.00", "tax_rate": "13%"},
    ],
    "acceptance": [
        {"doc_type": "GR", "doc_no": "GR-001", "line_no": 1, "sku": "SKU-A", "qty": "6", "amount": "600.00", "tax_rate": "13%"},
        {"doc_type": "GR", "doc_no": "GR-002", "line_no": 1, "sku": "SKU-A", "qty": "4", "amount": "400.00", "tax_rate": "13%"},
        {"doc_type": "GR", "doc_no": "GR-003", "line_no": 1, "sku": "SKU-B", "qty": "8", "amount": "800.00", "tax_rate": "9%"},
        {"doc_type": "GR", "doc_no": "GR-004", "line_no": 1, "sku": "SKU-C", "qty": "2", "amount": "200.00", "tax_rate": "13%"},
    ],
    "tax": [
        {"doc_type": "IV", "doc_no": "IV-001", "line_no": 1, "sku": "SKU-A", "qty": "10", "amount": "1130.00", "tax_rate": "13%"},
        {"doc_type": "IV", "doc_no": "IV-002", "line_no": 1, "sku": "SKU-B", "qty": "8", "amount": "872.00", "tax_rate": "9%"},
    ],
}


def sample_batch() -> dict:
    return open_batch(SAMPLE)
