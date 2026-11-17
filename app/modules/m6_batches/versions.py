"""Immutable, content-addressed versions of the three reconciliation facts.

A version is a frozen snapshot of either the purchase order, the acceptance
(goods receipt / return) or the tax (invoice declaration) dataset.  Versions
never change after creation; only the per-scope head pointer moves.  A batch
binds the three version ids it was opened with, which is what lets old
registered invoice evidence keep its original three-way criteria.
"""

import json
from hashlib import sha256
from decimal import Decimal, InvalidOperation

from app.config import RULE_VERSION
from app.errors import DomainError
from app.store import connect, now_iso, transaction
from app.modules.m1_documents.money import decimal_value, normalize_rate
from app.modules.m4_tax.calculate import money

SCOPES = ("order", "acceptance", "tax")
DEFAULT_DOC_TYPE = {"order": "PO", "acceptance": "GR", "tax": "IV"}
DOC_TYPES = {"order": {"PO"}, "acceptance": {"GR", "RT"}, "tax": {"IV"}}


def canonical_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _quantity(value) -> Decimal:
    try:
        number = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, AttributeError) as exc:
        raise DomainError("BAD_VERSION_INPUT", f"数量格式不正确: {value!r}") from exc
    if not number.is_finite():
        raise DomainError("BAD_VERSION_INPUT", f"数量格式不正确: {value!r}")
    return number


def _amount(value) -> Decimal:
    try:
        number = decimal_value(str(value))
    except ValueError as exc:
        raise DomainError("BAD_VERSION_INPUT", f"金额格式不正确: {value!r}") from exc
    return money(number)


def normalize_line(row: dict, scope: str, index: int) -> dict:
    if not isinstance(row, dict):
        raise DomainError("BAD_VERSION_INPUT", "数据行必须是对象")
    doc_type = str(row.get("doc_type") or DEFAULT_DOC_TYPE[scope]).strip().upper()
    if doc_type not in DOC_TYPES[scope]:
        raise DomainError("DOC_TYPE_MISMATCH", f"{scope} 版本不接受单据类型 {doc_type}")
    doc_no = str(row.get("doc_no") or "").strip()
    sku = str(row.get("sku") or "").strip()
    raw_rate = row.get("tax_rate")
    missing = [name for name, value in (("doc_no", doc_no), ("sku", sku), ("tax_rate", raw_rate)) if not str(value or "").strip()]
    if row.get("qty") in (None, "") or row.get("amount") in (None, ""):
        missing.append("qty/amount")
    if missing:
        raise DomainError("EMPTY_VALUE", f"版本行缺少必填值: {', '.join(missing)}")
    try:
        line_no = int(row.get("line_no", index))
    except (TypeError, ValueError) as exc:
        raise DomainError("BAD_VERSION_INPUT", f"行号格式不正确: {row.get('line_no')!r}") from exc
    if line_no < 1:
        raise DomainError("BAD_VERSION_INPUT", f"行号必须从 1 开始: {line_no}")
    try:
        rate = normalize_rate(str(raw_rate))
    except (InvalidOperation, ValueError) as exc:
        raise DomainError("BAD_VERSION_INPUT", f"税率格式不正确: {raw_rate!r}") from exc
    return {
        "doc_type": doc_type,
        "doc_no": doc_no,
        "line_no": line_no,
        "sku": sku,
        "tax_rate": canonical_decimal(rate),
        "qty": canonical_decimal(_quantity(row.get("qty"))),
        "amount": str(_amount(row.get("amount"))),
    }


def normalize_lines(rows: list[dict], scope: str) -> list[dict]:
    if scope not in SCOPES:
        raise DomainError("UNKNOWN_SCOPE", f"未知数据版本类型: {scope}")
    normalized = [normalize_line(row, scope, index) for index, row in enumerate(rows, start=1)]
    normalized.sort(key=lambda line: (line["doc_type"], line["doc_no"], line["line_no"]))
    seen = set()
    for line in normalized:
        identity = (line["doc_type"], line["doc_no"], line["line_no"])
        if identity in seen:
            raise DomainError("DUPLICATE_KEY", f"版本内行号重复: {identity}")
        seen.add(identity)
    return normalized


def version_digest(scope: str, lines: list[dict], rule_version: str = RULE_VERSION) -> str:
    payload = {"scope": scope, "rule_version": rule_version, "lines": lines}
    return sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def create_version(db, scope: str, rows: list[dict], rule_version: str = RULE_VERSION) -> dict:
    """Insert a version inside an existing transaction; reuse identical content."""
    lines = normalize_lines(rows, scope)
    digest = version_digest(scope, lines, rule_version)
    existing = db.execute(
        "SELECT version_id, version_no FROM dataset_versions WHERE scope = ? AND input_sha256 = ?",
        (scope, digest),
    ).fetchone()
    if existing:
        return {"version_id": existing["version_id"], "version_no": existing["version_no"], "input_sha256": digest, "created": False}
    version_no = db.execute("SELECT COALESCE(MAX(version_no), 0) + 1 AS next FROM dataset_versions WHERE scope = ?", (scope,)).fetchone()["next"]
    version_id = f"{scope}-v{digest[:12]}"
    db.execute(
        "INSERT INTO dataset_versions (version_id, scope, version_no, input_sha256, rule_version, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (version_id, scope, version_no, digest, rule_version, now_iso()),
    )
    for line in lines:
        db.execute(
            "INSERT INTO dataset_lines (version_id, doc_type, doc_no, line_no, sku, tax_rate, qty, amount, kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (version_id, line["doc_type"], line["doc_no"], line["line_no"], line["sku"], line["tax_rate"], line["qty"], line["amount"], "return" if line["doc_type"] == "RT" else "normal"),
        )
    return {"version_id": version_id, "version_no": version_no, "input_sha256": digest, "created": True}


def stage_version(scope: str, rows: list[dict], rule_version: str = RULE_VERSION) -> dict:
    """Create (or reuse) an immutable version without moving the scope head.

    Versions only ever advance through a batch, so a failed order revision or
    an interrupted batch cannot leave the head on an uncommitted snapshot.
    """
    with transaction() as db:
        summary = create_version(db, scope, rows, rule_version)
        current = head(db, scope)
    return {**summary, "scope": scope, "head": current}


def head(db, scope: str) -> str | None:
    row = db.execute("SELECT version_id FROM dataset_heads WHERE scope = ?", (scope,)).fetchone()
    return row["version_id"] if row else None


def get_version(db, version_id: str):
    return db.execute("SELECT * FROM dataset_versions WHERE version_id = ?", (version_id,)).fetchone()


def lines(db, version_id: str) -> list[dict]:
    return [dict(row) for row in db.execute(
        "SELECT doc_type, doc_no, line_no, sku, tax_rate, qty, amount, kind FROM dataset_lines WHERE version_id = ? "
        "ORDER BY doc_type, doc_no, line_no",
        (version_id,),
    )]


def read_version(version_id: str) -> dict:
    with connect() as db:
        version = get_version(db, version_id)
        if not version:
            raise DomainError("MISSING_VERSION", f"数据版本不存在: {version_id}", 404)
        return {**dict(version), "lines": lines(db, version_id)}


def state() -> dict:
    with connect() as db:
        heads = {row["scope"]: row["version_id"] for row in db.execute("SELECT scope, version_id FROM dataset_heads")}
        versions = [dict(row) for row in db.execute("SELECT * FROM dataset_versions ORDER BY scope, version_no")]
    return {"heads": heads, "versions": versions}
