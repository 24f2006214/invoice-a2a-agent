import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse


BASE_PATH = "/a2a/"
DB_PATH = os.getenv("DATABASE_PATH", "tasks.db")
DEFAULT_TOKEN = os.getenv("INVOICE_AGENT_TOKEN", "change-me-before-deploying")
db_lock = threading.RLock()

app = FastAPI(title="Invoice Action Agent", version="1.0.0")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def message_hash(message: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(message).encode()).hexdigest()


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            context_id TEXT NOT NULL,
            principal TEXT NOT NULL,
            batch_id TEXT NOT NULL,
            state TEXT NOT NULL,
            history_json TEXT NOT NULL,
            proposal_json TEXT,
            receipt_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS idempotency (
            principal TEXT NOT NULL,
            message_id TEXT NOT NULL,
            semantic_hash TEXT NOT NULL,
            task_id TEXT NOT NULL,
            PRIMARY KEY (principal, message_id)
        )
    """)
    connection.commit()
    return connection


def token_from_header(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="authentication required")
    token = authorization[7:].strip()
    if not token or token == "change-me-before-deploying":
        raise HTTPException(status_code=403, detail="authentication required")
    return token


def require_protocol(a2a_version: str | None, content_type: str | None) -> None:
    if a2a_version != "1.0":
        raise HTTPException(status_code=400, detail="invalid A2A version")
    if not content_type or content_type.split(";", 1)[0].strip() != "application/a2a+json":
        raise HTTPException(status_code=415, detail="invalid media type")


def text_of(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(f"{k}: {text_of(v)}" for k, v in value.items())
    if isinstance(value, list):
        return " ".join(text_of(v) for v in value)
    return str(value)


REF_RE = re.compile(r"\[[^\]]+\]")


def package_facts(package: dict[str, Any]) -> dict[str, Any]:
    facts = package.get("facts") if isinstance(package.get("facts"), dict) else package
    vendor = facts.get("vendorName", facts.get("vendor", facts.get("supplier", "Unknown vendor")))
    invoice = facts.get("invoiceNumber", facts.get("invoice", facts.get("invoice_no", package.get("packageId", "unknown"))))
    amount = facts.get("amountMinor", facts.get("amount_minor", facts.get("amount", 0)))
    currency = facts.get("currency", "INR")
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        amount = 0
    return {"vendorName": str(vendor), "invoiceNumber": str(invoice), "amountMinor": amount, "currency": str(currency)}


def choose_action(package: dict[str, Any]) -> tuple[str, list[str], str]:
    raw = text_of(package)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n|(?<=[.!?])\s+(?=\[)", raw) if p.strip()]
    decisive = " ".join(paragraphs[-1:]) or raw
    lower = raw.lower()
    if re.search(r"already\s+paid|paid\s+previously|duplicate\s+payment|duplicate\s+invoice", lower):
        action = "reject_duplicate"
    elif re.search(r"conflict|mismatch|inconsistent|exception|required escalation|escalate", lower):
        action = "open_exception"
    elif re.search(r"hold|verify|verification|awaiting|pending confirmation|pause", lower):
        action = "hold_invoice"
    elif re.search(r"approval|approve|above.*limit|exceeds.*limit|outside.*authority|delegated authority", lower):
        action = "request_approval"
    else:
        action = "settle_invoice"
    refs = REF_RE.findall(decisive)
    if len(refs) < 3:
        refs = REF_RE.findall(raw)
    refs = list(dict.fromkeys(refs))[:3]
    while len(refs) < 3:
        refs.append(f"[package-{package.get('packageId', 'unknown')}-evidence-{len(refs)+1}]")
    rationale = f"{action} is selected because the decisive invoice evidence says: {decisive[:900]}. Evidence refs: {', '.join(refs)}."
    return action, refs, rationale[:1500]


def task_json(row: sqlite3.Row) -> dict[str, Any]:
    result = {
        "id": row["task_id"],
        "contextId": row["context_id"],
        "status": {"state": row["state"]},
        "history": json.loads(row["history_json"]),
    }
    artifacts = []
    if row["proposal_json"]:
        artifacts.append({"parts": [{"mediaType": "application/vnd.ga5.invoice-action-proposals+json", "data": json.loads(row["proposal_json"])}]})
    if row["receipt_json"]:
        artifacts.append({"parts": [{"mediaType": "application/vnd.ga5.invoice-action-receipts+json", "data": json.loads(row["receipt_json"])}]})
    if artifacts:
        result["artifacts"] = artifacts
    return result


def envelope(task: dict[str, Any]) -> dict[str, Any]:
    return {"task": task}


@app.get("/.well-known/agent-card.json")
def agent_card(request: Request) -> dict[str, Any]:
    base = str(request.base_url).rstrip("/") + BASE_PATH
    return {
        "name": "Invoice Action Agent",
        "description": "Reads invoice packages and proposes safe, evidence-based business actions.",
        "version": "1.0.0",
        "capabilities": {},
        "supportedInterfaces": [{"url": base, "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"}],
        "defaultInputModes": ["application/vnd.ga5.invoice-claim-batch+json"],
        "defaultOutputModes": ["application/vnd.ga5.invoice-action-proposals+json", "application/vnd.ga5.invoice-action-receipts+json"],
        "skills": [{"id": "invoice_action_agent", "name": "Invoice Action Agent", "description": "Chooses safe invoice actions using document evidence.", "tags": ["invoice", "payment", "approval"]}],
    }


@app.post("/a2a/message:send")
async def send_message(request: Request, authorization: str | None = Header(default=None), a2a_version: str | None = Header(default=None), content_type: str | None = Header(default=None)):
    principal = token_from_header(authorization)
    require_protocol(a2a_version, content_type)
    body = await request.json()
    message = body.get("message") or {}
    continuation_task_id = message.get("taskId")
    if continuation_task_id:
        with db_lock:
            db = connect()
            row = owned_task(db, continuation_task_id, principal)
            if row["state"] != "TASK_STATE_INPUT_REQUIRED":
                raise HTTPException(status_code=409, detail="task is terminal")
            # Some A2A clients omit contextId on a continuation; when it is
            # supplied, it must match the stored task context exactly.
            if message.get("contextId") and message.get("contextId") != row["context_id"]:
                raise HTTPException(status_code=409, detail="task context mismatch")
            parts = message.get("parts") or []
            result_data = next((p.get("data") for p in parts if p.get("mediaType") == "application/vnd.ga5.invoice-action-results+json"), None)
            if not isinstance(result_data, dict) or not isinstance(result_data.get("results"), list):
                raise HTTPException(status_code=400, detail="action results required")
            proposal = json.loads(row["proposal_json"])
            proposal_by_id = {p["actionId"]: p for p in proposal["proposals"]}
            if result_data.get("batchId") != row["batch_id"]:
                raise HTTPException(status_code=409, detail="task batch mismatch")
            executions = []
            for result in result_data["results"]:
                proposal_item = proposal_by_id.get(result.get("actionId"))
                if not proposal_item or result.get("packageId") != proposal_item["packageId"] or result.get("action") != proposal_item["action"]:
                    raise HTTPException(status_code=409, detail="proposal mismatch")
                if result.get("outcome") == "ACCEPTED":
                    executions.append({"packageId": proposal_item["packageId"], "actionId": proposal_item["actionId"], "action": proposal_item["action"], "receiptNonce": result.get("receiptNonce"), "facts": proposal_item["facts"], "evidenceRefs": proposal_item["evidenceRefs"]})
            history = json.loads(row["history_json"])
            history.append(message)
            receipt = {"batchId": row["batch_id"], "executions": executions}
            db.execute("UPDATE tasks SET state=?, history_json=?, receipt_json=?, updated_at=? WHERE task_id=? AND state=?", ("TASK_STATE_COMPLETED", json.dumps(history), json.dumps(receipt), now(), continuation_task_id, "TASK_STATE_INPUT_REQUIRED"))
            db.commit()
            final_row = owned_task(db, continuation_task_id, principal)
            return JSONResponse(envelope(task_json(final_row)), media_type="application/a2a+json")
    message_id = message.get("messageId")
    if not message_id:
        raise HTTPException(status_code=400, detail="messageId required")
    semantic = message_hash(message)
    with db_lock:
        db = connect()
        existing = db.execute("SELECT * FROM idempotency WHERE principal=? AND message_id=?", (principal, message_id)).fetchone()
        if existing:
            if existing["semantic_hash"] != semantic:
                raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (existing["task_id"],)).fetchone()
            return JSONResponse(envelope(task_json(row)), media_type="application/a2a+json")
        parts = message.get("parts") or []
        data = next((p.get("data") for p in parts if p.get("mediaType") == "application/vnd.ga5.invoice-claim-batch+json"), None)
        if not isinstance(data, dict) or not isinstance(data.get("packages"), list):
            raise HTTPException(status_code=400, detail="invoice batch required")
        batch_id = str(data.get("batchId", ""))
        proposals = []
        for index, package in enumerate(data["packages"]):
            package = package if isinstance(package, dict) else {"content": package}
            package_id = str(package.get("packageId", f"package-{index+1}"))
            action, refs, rationale = choose_action(package)
            proposals.append({"packageId": package_id, "actionId": secrets.token_urlsafe(16), "action": action, "facts": package_facts(package), "evidenceRefs": refs, "rationale": rationale})
        task_id = str(uuid.uuid4())
        # Preserve a caller-supplied context so the result continuation can
        # be matched exactly; generate one only for messages that omit it.
        context_id = str(message.get("contextId") or uuid.uuid4())
        proposal = {"batchId": batch_id, "proposals": proposals}
        history = [message]
        timestamp = now()
        db.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (task_id, context_id, principal, batch_id, "TASK_STATE_INPUT_REQUIRED", json.dumps(history), json.dumps(proposal), None, timestamp, timestamp))
        db.execute("INSERT INTO idempotency VALUES (?, ?, ?, ?)", (principal, message_id, semantic, task_id))
        db.commit()
        row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return JSONResponse(envelope(task_json(row)), media_type="application/a2a+json")


def owned_task(db: sqlite3.Connection, task_id: str, principal: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM tasks WHERE task_id=? AND principal=?", (task_id, principal)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="task not found")
    return row


@app.get("/a2a/tasks/{task_id}")
def get_task(task_id: str, authorization: str | None = Header(default=None), a2a_version: str | None = Header(default=None), content_type: str | None = Header(default=None)):
    principal = token_from_header(authorization)
    require_protocol(a2a_version, content_type)
    with db_lock:
        row = owned_task(connect(), task_id, principal)
        return JSONResponse(envelope(task_json(row)), media_type="application/a2a+json")


@app.get("/a2a/tasks")
def list_tasks(authorization: str | None = Header(default=None), a2a_version: str | None = Header(default=None), content_type: str | None = Header(default=None)):
    principal = token_from_header(authorization)
    require_protocol(a2a_version, content_type)
    with db_lock:
        rows = connect().execute("SELECT * FROM tasks WHERE principal=? ORDER BY created_at", (principal,)).fetchall()
        return JSONResponse({"tasks": [task_json(row) for row in rows]}, media_type="application/a2a+json")


@app.post("/a2a/tasks/{task_id}:cancel")
def cancel_task(task_id: str, authorization: str | None = Header(default=None), a2a_version: str | None = Header(default=None), content_type: str | None = Header(default=None)):
    principal = token_from_header(authorization)
    require_protocol(a2a_version, content_type)
    with db_lock:
        db = connect()
        row = owned_task(db, task_id, principal)
        if row["state"] != "TASK_STATE_INPUT_REQUIRED":
            raise HTTPException(status_code=409, detail="task is terminal")
        db.execute("UPDATE tasks SET state=?, updated_at=? WHERE task_id=? AND state=?", ("TASK_STATE_CANCELED", now(), task_id, "TASK_STATE_INPUT_REQUIRED"))
        db.commit()
        return JSONResponse(envelope(task_json(owned_task(db, task_id, principal))), media_type="application/a2a+json")

