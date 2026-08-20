from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timedelta
import uuid
import os
import json
import psycopg2
import psycopg2.extras
import httpx

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

PDA_URL = "https://katina-bot-1.onrender.com"
DATABASE_URL = os.environ.get("DATABASE_URL")

WAITER_TOKEN = os.environ.get("WAITER_TOKEN", "changeme")

QUEUE_DELAY_MINUTES_PER_ORDER = 2


class MenuItemExtra(BaseModel):
    name: str
    price: float


class MenuItemCreate(BaseModel):
    item_id: str
    name: str
    price: float
    description: str = ""
    image: str = ""
    category: str = "other"
    extras: List[MenuItemExtra] = []
    prep_time_minutes: int = 10


class MenuItemUpdate(BaseModel):
    name: str = None
    price: float = None
    description: str = None
    image: str = None
    available: int = None
    category: str = None
    extras: Optional[List[MenuItemExtra]] = None
    prep_time_minutes: int = None


def verify_waiter(x_waiter_token: str = Header(None)):
    if x_waiter_token != WAITER_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing waiter token")


def get_waiter_name(x_waiter_name: str = Header(None)) -> str:
    return x_waiter_name or "Unknown"


def get_connection():
    conn = psycopg2.connect(DATABASE_URL)
    return conn


def get_cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def log_action(waiter_name: str, action: str, target_id: str):
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute(
        "INSERT INTO action_log (waiter_name, action, target_id) VALUES (%s, %s, %s)",
        (waiter_name, action, target_id),
    )
    conn.commit()
    cur.close()
    conn.close()


def parse_extras(raw):
    try:
        return json.loads(raw) if raw else []
    except Exception:
        return []


def format_order(order_dict):
    if order_dict.get("estimated_ready_at"):
        val = order_dict["estimated_ready_at"]
        if hasattr(val, "isoformat"):
            order_dict["estimated_ready_at"] = val.isoformat() + "Z"
    order_dict["extras"] = parse_extras(order_dict.get("extras"))
    return order_dict


def init_db():
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tables (
            table_id TEXT PRIMARY KEY,
            display_label TEXT,
            active_session_id TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            table_id TEXT,
            party_size INTEGER,
            status TEXT,
            is_paid INTEGER DEFAULT 0,
            waiter_called INTEGER DEFAULT 0,
            payment_requested INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            session_id TEXT,
            item TEXT,
            qty INTEGER,
            price REAL,
            status TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS action_log (
            id SERIAL PRIMARY KEY,
            waiter_name TEXT,
            action TEXT,
            target_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS menu_items (
            item_id TEXT PRIMARY KEY,
            name TEXT,
            price REAL,
            description TEXT,
            image TEXT,
            available INTEGER DEFAULT 1
        )
    """)
    conn.commit()

    for column_def in ["waiter_called INTEGER DEFAULT 0", "payment_requested INTEGER DEFAULT 0"]:
        try:
            cur.execute(f"ALTER TABLE sessions ADD COLUMN {column_def}")
            conn.commit()
        except psycopg2.errors.DuplicateColumn:
            conn.rollback()

    try:
        cur.execute("ALTER TABLE sessions ADD COLUMN payment_method TEXT")
        conn.commit()
    except psycopg2.errors.DuplicateColumn:
        conn.rollback()

    try:
        cur.execute("ALTER TABLE orders ADD COLUMN idempotency_key TEXT")
        conn.commit()
    except psycopg2.errors.DuplicateColumn:
        conn.rollback()

    try:
        cur.execute("ALTER TABLE orders ADD COLUMN handled_by TEXT")
        conn.commit()
    except psycopg2.errors.DuplicateColumn:
        conn.rollback()

    try:
        cur.execute("ALTER TABLE orders ADD COLUMN note TEXT DEFAULT ''")
        conn.commit()
    except psycopg2.errors.DuplicateColumn:
        conn.rollback()

    try:
        cur.execute("ALTER TABLE orders ADD COLUMN extras TEXT DEFAULT '[]'")
        conn.commit()
    except psycopg2.errors.DuplicateColumn:
        conn.rollback()

    try:
        cur.execute("ALTER TABLE orders ADD COLUMN ready INTEGER DEFAULT 0")
        conn.commit()
    except psycopg2.errors.DuplicateColumn:
        conn.rollback()

    try:
        cur.execute("ALTER TABLE orders ADD COLUMN estimated_ready_at TIMESTAMP")
        conn.commit()
    except psycopg2.errors.DuplicateColumn:
        conn.rollback()

    try:
        cur.execute("ALTER TABLE menu_items ADD COLUMN category TEXT DEFAULT 'other'")
        conn.commit()
    except psycopg2.errors.DuplicateColumn:
        conn.rollback()

    try:
        cur.execute("ALTER TABLE menu_items ADD COLUMN extras TEXT DEFAULT '[]'")
        conn.commit()
    except psycopg2.errors.DuplicateColumn:
        conn.rollback()

    try:
        cur.execute("ALTER TABLE menu_items ADD COLUMN prep_time_minutes INTEGER DEFAULT 10")
        conn.commit()
    except psycopg2.errors.DuplicateColumn:
        conn.rollback()

    try:
        cur.execute("""
            CREATE UNIQUE INDEX idx_orders_idempotency
            ON orders (idempotency_key) WHERE idempotency_key IS NOT NULL
        """)
        conn.commit()
    except psycopg2.errors.DuplicateTable:
        conn.rollback()

    seed_items = [
        ("coke", "coke", 3.50, "Ice-cold classic, served in a chilled glass", "https://katina-bot-2.onrender.com/image/coke.jpg", "drinks"),
        ("burger", "burger", 9.90, "Juicy beef patty, cheddar, house sauce, brioche bun", "https://katina-bot-2.onrender.com/image/burger.jpg", "burgers"),
        ("salad", "salad", 7.20, "Crisp greens, feta, olives, house vinaigrette", "https://katina-bot-2.onrender.com/image/salad.jpg", "salads"),
    ]
    for item_id, name, price, description, image, category in seed_items:
        cur.execute(
            "INSERT INTO menu_items (item_id, name, price, description, image, available, category) VALUES (%s, %s, %s, %s, %s, 1, %s) ON CONFLICT (item_id) DO NOTHING",
            (item_id, name, price, description, image, category),
        )
    conn.commit()

    cur.close()
    conn.close()


init_db()


@app.get("/")
def root():
    return {"status": "alive"}


@app.get("/verify-token")
def verify_token(_auth: None = Depends(verify_waiter)):
    return {"valid": True}


@app.get("/menu")
def get_menu():
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM menu_items WHERE available = 1")
    items = cur.fetchall()
    cur.close()
    conn.close()

    menu = {}
    for item in items:
        menu[item["item_id"]] = {
            "price": item["price"],
            "description": item["description"],
            "image": item["image"],
            "category": item["category"] or "other",
            "extras": parse_extras(item.get("extras")),
            "prep_time_minutes": item.get("prep_time_minutes") or 10,
        }
    return menu


@app.get("/activity-log")
def get_activity_log(_auth: None = Depends(verify_waiter)):
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM action_log ORDER BY created_at DESC LIMIT 50")
    logs = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(l) for l in logs]


@app.get("/revenue-stats")
def get_revenue_stats(_auth: None = Depends(verify_waiter)):
    conn = get_connection()
    cur = get_cursor(conn)

    cur.execute("SELECT COALESCE(SUM(price * qty), 0) as total FROM orders WHERE status = 'APPROVED'")
    total_revenue = cur.fetchone()["total"]

    cur.execute("""
        SELECT COALESCE(SUM(o.price * o.qty), 0) as unpaid
        FROM orders o
        JOIN sessions s ON o.session_id = s.session_id
        WHERE o.status = 'APPROVED' AND s.status = 'OPEN' AND s.is_paid = 0
    """)
    unpaid_amount = cur.fetchone()["unpaid"]

    cur.close()
    conn.close()
    return {"total_revenue": float(total_revenue), "unpaid_amount": float(unpaid_amount)}


# ---------- Menu Items ----------

@app.post("/menu-items")
def add_menu_item(payload: MenuItemCreate, _auth: None = Depends(verify_waiter), waiter_name: str = Depends(get_waiter_name)):
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM menu_items WHERE item_id = %s", (payload.item_id,))
    if cur.fetchone():
        cur.close()
        conn.close()
        return {"error": "Item already exists"}

    extras_json = json.dumps([e.dict() for e in payload.extras])

    cur.execute(
        "INSERT INTO menu_items (item_id, name, price, description, image, available, category, extras, prep_time_minutes) VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s)",
        (payload.item_id, payload.name, payload.price, payload.description, payload.image, payload.category, extras_json, payload.prep_time_minutes),
    )
    conn.commit()
    cur.close()
    conn.close()

    log_action(waiter_name, "added menu item", payload.item_id)

    return {"item_id": payload.item_id, "name": payload.name, "price": payload.price}


@app.get("/menu-items")
def list_menu_items():
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM menu_items ORDER BY category, name")
    items = cur.fetchall()
    cur.close()
    conn.close()

    result = []
    for i in items:
        d = dict(i)
        d["extras"] = parse_extras(d.get("extras"))
        result.append(d)
    return result


@app.post("/menu-items/{item_id}/update")
def update_menu_item(item_id: str, payload: MenuItemUpdate, _auth: None = Depends(verify_waiter), waiter_name: str = Depends(get_waiter_name)):
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM menu_items WHERE item_id = %s", (item_id,))
    item = cur.fetchone()

    if item is None:
        cur.close()
        conn.close()
        return {"error": "Item not found"}

    extras_json = json.dumps([e.dict() for e in payload.extras]) if payload.extras is not None else item["extras"]

    cur.execute(
        "UPDATE menu_items SET name=%s, price=%s, description=%s, image=%s, available=%s, category=%s, extras=%s, prep_time_minutes=%s WHERE item_id=%s",
        (
            payload.name if payload.name is not None else item["name"],
            payload.price if payload.price is not None else item["price"],
            payload.description if payload.description is not None else item["description"],
            payload.image if payload.image is not None else item["image"],
            payload.available if payload.available is not None else item["available"],
            payload.category if payload.category is not None else item["category"],
            extras_json,
            payload.prep_time_minutes if payload.prep_time_minutes is not None else item.get("prep_time_minutes", 10),
            item_id,
        ),
    )
    conn.commit()
    cur.close()
    conn.close()

    log_action(waiter_name, "updated menu item", item_id)

    return {"item_id": item_id, "updated": True}


@app.delete("/menu-items/{item_id}")
def delete_menu_item(item_id: str, _auth: None = Depends(verify_waiter), waiter_name: str = Depends(get_waiter_name)):
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("DELETE FROM menu_items WHERE item_id = %s", (item_id,))
    conn.commit()
    cur.close()
    conn.close()

    log_action(waiter_name, "deleted menu item", item_id)

    return {"item_id": item_id, "deleted": True}


# ---------- Tables (permanent) ----------

@app.post("/tables")
def create_table(table_id: str, display_label: str = None, _auth: None = Depends(verify_waiter), waiter_name: str = Depends(get_waiter_name)):
    if not table_id or not table_id.strip():
        return {"error": "Table id cannot be empty"}

    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM tables WHERE table_id = %s", (table_id,))
    if cur.fetchone():
        cur.close()
        conn.close()
        return {"error": "Table already exists"}

    cur.execute(
        "INSERT INTO tables (table_id, display_label, active_session_id) VALUES (%s, %s, NULL)",
        (table_id, display_label or table_id),
    )
    conn.commit()
    cur.close()
    conn.close()

    log_action(waiter_name, "registered table", table_id)

    return {"table_id": table_id, "display_label": display_label or table_id}


@app.get("/tables")
def list_tables():
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM tables")
    tables = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(t) for t in tables]


@app.post("/tables/{table_id}/rename")
def rename_table(table_id: str, display_label: str, _auth: None = Depends(verify_waiter), waiter_name: str = Depends(get_waiter_name)):
    if not display_label or not display_label.strip():
        return {"error": "Name cannot be empty"}

    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM tables WHERE table_id = %s", (table_id,))
    if cur.fetchone() is None:
        cur.close()
        conn.close()
        return {"error": "Table not found"}

    cur.execute("UPDATE tables SET display_label = %s WHERE table_id = %s", (display_label, table_id))
    conn.commit()
    cur.close()
    conn.close()

    log_action(waiter_name, "renamed table", table_id)

    return {"table_id": table_id, "display_label": display_label}


@app.get("/tables/{table_id}/history")
def get_table_history(table_id: str, _auth: None = Depends(verify_waiter)):
    conn = get_connection()
    cur = get_cursor(conn)

    cur.execute("SELECT session_id FROM sessions WHERE table_id = %s", (table_id,))
    session_ids = [row["session_id"] for row in cur.fetchall()]

    order_ids = []
    if session_ids:
        cur.execute("SELECT order_id FROM orders WHERE session_id = ANY(%s)", (session_ids,))
        order_ids = [row["order_id"] for row in cur.fetchall()]

    target_ids = [table_id] + session_ids + order_ids

    cur.execute(
        "SELECT * FROM action_log WHERE target_id = ANY(%s) ORDER BY created_at DESC LIMIT 50",
        (target_ids,),
    )
    logs = cur.fetchall()
    cur.close()
    conn.close()

    return [dict(l) for l in logs]


@app.delete("/tables/{table_id}")
def delete_table(table_id: str, _auth: None = Depends(verify_waiter), waiter_name: str = Depends(get_waiter_name)):
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM tables WHERE table_id = %s", (table_id,))
    table = cur.fetchone()

    if table is None:
        cur.close()
        conn.close()
        return {"error": "Table not found"}

    if table["active_session_id"] is not None:
        cur.close()
        conn.close()
        return {"error": "Cannot delete a table with an active session — release it first"}

    cur.execute("DELETE FROM tables WHERE table_id = %s", (table_id,))
    conn.commit()
    cur.close()
    conn.close()

    log_action(waiter_name, "deleted table", table_id)

    return {"table_id": table_id, "deleted": True}


@app.get("/tables/{table_id}/active-session")
def get_active_session(table_id: str):
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM tables WHERE table_id = %s", (table_id,))
    table = cur.fetchone()
    cur.close()
    conn.close()

    if table is None or table["active_session_id"] is None:
        return {"active": False}
    return {"active": True, "session_id": table["active_session_id"]}


@app.post("/tables/{table_id}/release")
def release_table(table_id: str, _auth: None = Depends(verify_waiter), waiter_name: str = Depends(get_waiter_name)):
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM tables WHERE table_id = %s", (table_id,))
    table = cur.fetchone()

    if table is None:
        cur.close()
        conn.close()
        return {"error": "Table not found"}

    if table["active_session_id"] is None:
        cur.close()
        conn.close()
        return {"error": "Table has no active session"}

    cur.execute("SELECT * FROM sessions WHERE session_id = %s", (table["active_session_id"],))
    session = cur.fetchone()

    if session and session["status"] not in ("CLOSED", "DELETED"):
        cur.close()
        conn.close()
        return {"error": "Session must be closed before releasing the table"}

    cur.execute("UPDATE tables SET active_session_id = NULL WHERE table_id = %s", (table_id,))
    conn.commit()
    cur.close()
    conn.close()

    log_action(waiter_name, "released table", table_id)

    return {"table_id": table_id, "released": True}


# ---------- Sessions ----------

@app.get("/sessions/{session_id}")
def get_session(session_id: str):
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM sessions WHERE session_id = %s", (session_id,))
    session = cur.fetchone()
    cur.close()
    conn.close()

    if session is None:
        return {"error": "Session not found"}
    return dict(session)


@app.get("/sessions")
def list_sessions(status: str = "OPEN"):
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM sessions WHERE status = %s", (status,))
    sessions = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(s) for s in sessions]


@app.post("/sessions/open")
def open_session(table_id: str, party_size: int, _auth: None = Depends(verify_waiter), waiter_name: str = Depends(get_waiter_name)):
    if party_size < 1:
        return {"error": "Party size must be at least 1"}

    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM tables WHERE table_id = %s", (table_id,))
    table = cur.fetchone()

    if table is None:
        cur.close()
        conn.close()
        return {"error": "Table not found. Create it first with POST /tables"}

    if table["active_session_id"] is not None:
        cur.close()
        conn.close()
        return {"error": "Table already has an active session"}

    session_id = f"s_{uuid.uuid4().hex[:8]}"
    cur.execute(
        "INSERT INTO sessions (session_id, table_id, party_size, status, is_paid) VALUES (%s, %s, %s, %s, %s)",
        (session_id, table_id, party_size, "OPEN", 0),
    )
    cur.execute("UPDATE tables SET active_session_id = %s WHERE table_id = %s", (session_id, table_id))
    conn.commit()
    cur.close()
    conn.close()

    log_action(waiter_name, "opened session", session_id)

    return {
        "session_id": session_id,
        "table_id": table_id,
        "party_size": party_size,
        "status": "OPEN",
        "is_paid": False,
    }


@app.post("/sessions/{session_id}/update-party-size")
def update_party_size(session_id: str, party_size: int, _auth: None = Depends(verify_waiter), waiter_name: str = Depends(get_waiter_name)):
    if party_size < 1:
        return {"error": "Party size must be at least 1"}

    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM sessions WHERE session_id = %s", (session_id,))
    session = cur.fetchone()

    if session is None:
        cur.close()
        conn.close()
        return {"error": "Session not found"}

    if session["status"] != "OPEN":
        cur.close()
        conn.close()
        return {"error": "Cannot update party size on a closed session"}

    cur.execute(
        "UPDATE sessions SET party_size = %s WHERE session_id = %s",
        (party_size, session_id),
    )
    conn.commit()
    cur.close()
    conn.close()

    log_action(waiter_name, "updated party size", session_id)

    return {"session_id": session_id, "party_size": party_size}


@app.post("/sessions/{session_id}/move")
def move_session(session_id: str, to_table_id: str, _auth: None = Depends(verify_waiter), waiter_name: str = Depends(get_waiter_name)):
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM sessions WHERE session_id = %s", (session_id,))
    session = cur.fetchone()

    if session is None:
        cur.close()
        conn.close()
        return {"error": "Session not found"}

    if session["status"] != "OPEN":
        cur.close()
        conn.close()
        return {"error": "Session is not open"}

    if session["table_id"] == to_table_id:
        cur.close()
        conn.close()
        return {"error": "Already at this table"}

    cur.execute("SELECT * FROM tables WHERE table_id = %s", (to_table_id,))
    target = cur.fetchone()

    if target is None:
        cur.close()
        conn.close()
        return {"error": "Target table not found"}

    if target["active_session_id"] is not None:
        cur.close()
        conn.close()
        return {"error": "Target table is not empty"}

    old_table_id = session["table_id"]
    cur.execute("UPDATE sessions SET table_id = %s WHERE session_id = %s", (to_table_id, session_id))
    cur.execute("UPDATE tables SET active_session_id = NULL WHERE table_id = %s", (old_table_id,))
    cur.execute("UPDATE tables SET active_session_id = %s WHERE table_id = %s", (session_id, to_table_id))
    conn.commit()
    cur.close()
    conn.close()

    log_action(waiter_name, "moved table", session_id)

    return {"session_id": session_id, "from_table_id": old_table_id, "to_table_id": to_table_id, "moved": True}


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str, _auth: None = Depends(verify_waiter), waiter_name: str = Depends(get_waiter_name)):
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM sessions WHERE session_id = %s", (session_id,))
    session = cur.fetchone()

    if session is None:
        cur.close()
        conn.close()
        return {"error": "Session not found"}

    if session["status"] != "OPEN":
        cur.close()
        conn.close()
        return {"error": "Session already closed or deleted"}

    cur.execute("UPDATE sessions SET status = %s WHERE session_id = %s", ("DELETED", session_id))
    cur.execute(
        "UPDATE orders SET status = 'CANCELLED' WHERE session_id = %s AND status != 'CANCELLED'",
        (session_id,),
    )
    conn.commit()
    cur.close()
    conn.close()

    log_action(waiter_name, "deleted session", session_id)

    return {"session_id": session_id, "status": "DELETED"}


@app.get("/sessions/{session_id}/orders")
def get_session_orders(session_id: str):
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM orders WHERE session_id = %s", (session_id,))
    orders = cur.fetchall()
    cur.close()
    conn.close()

    return [format_order(dict(o)) for o in orders]


@app.post("/sessions/{session_id}/split")
def calculate_split(session_id: str):
    conn = get_connection()
    cur = get_cursor(conn)

    cur.execute("SELECT * FROM sessions WHERE session_id = %s", (session_id,))
    session = cur.fetchone()

    if session is None:
        cur.close()
        conn.close()
        return {"error": "Session not found"}

    cur.execute(
        "SELECT * FROM orders WHERE session_id = %s AND status = 'APPROVED'",
        (session_id,),
    )
    approved_orders = cur.fetchall()
    cur.close()
    conn.close()

    total = sum(order["price"] * order["qty"] for order in approved_orders)
    party_size = session["party_size"]
    per_person = total / party_size

    return {
        "session_id": session_id,
        "total": total,
        "party_size": party_size,
        "per_person": round(per_person, 2),
    }


@app.post("/sessions/{session_id}/mark-paid")
def mark_paid(session_id: str, _auth: None = Depends(verify_waiter), waiter_name: str = Depends(get_waiter_name)):
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM sessions WHERE session_id = %s", (session_id,))
    session = cur.fetchone()

    if session is None:
        cur.close()
        conn.close()
        return {"error": "Session not found"}

    cur.execute(
        "UPDATE sessions SET is_paid = 1, payment_requested = 0, status = 'CLOSED' WHERE session_id = %s",
        (session_id,),
    )
    cur.execute("UPDATE tables SET active_session_id = NULL WHERE table_id = %s", (session["table_id"],))
    conn.commit()
    cur.close()
    conn.close()

    log_action(waiter_name, "marked paid and released table", session_id)

    return {"session_id": session_id, "is_paid": True, "released": True}


@app.post("/sessions/{session_id}/call-waiter")
def call_waiter(session_id: str):
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM sessions WHERE session_id = %s", (session_id,))
    if cur.fetchone() is None:
        cur.close()
        conn.close()
        return {"error": "Session not found"}

    cur.execute("UPDATE sessions SET waiter_called = 1 WHERE session_id = %s", (session_id,))
    conn.commit()
    cur.close()
    conn.close()
    return {"session_id": session_id, "waiter_called": True}


@app.post("/sessions/{session_id}/acknowledge-call")
def acknowledge_call(session_id: str, _auth: None = Depends(verify_waiter), waiter_name: str = Depends(get_waiter_name)):
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("UPDATE sessions SET waiter_called = 0 WHERE session_id = %s", (session_id,))
    conn.commit()
    cur.close()
    conn.close()

    log_action(waiter_name, "acknowledged call", session_id)

    return {"session_id": session_id, "waiter_called": False}


@app.post("/sessions/{session_id}/request-payment")
def request_payment(session_id: str, method: str = None):
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM sessions WHERE session_id = %s", (session_id,))
    if cur.fetchone() is None:
        cur.close()
        conn.close()
        return {"error": "Session not found"}

    cur.execute(
        "UPDATE sessions SET payment_requested = 1, payment_method = %s WHERE session_id = %s",
        (method, session_id),
    )
    conn.commit()
    cur.close()
    conn.close()
    return {"session_id": session_id, "payment_requested": True, "payment_method": method}


# ---------- Orders ----------

@app.get("/orders/pending")
def get_pending_orders():
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM orders WHERE status = 'PENDING_REVIEW'")
    orders = cur.fetchall()
    cur.close()
    conn.close()

    return [format_order(dict(o)) for o in orders]


@app.get("/orders/ready")
def get_ready_orders():
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM orders WHERE status = 'APPROVED' AND ready = 1")
    orders = cur.fetchall()
    cur.close()
    conn.close()

    return [format_order(dict(o)) for o in orders]


def get_menu_item_row(item: str):
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM menu_items WHERE item_id = %s AND available = 1", (item,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


@app.post("/orders")
def submit_order(session_id: str, item: str, qty: int, idempotency_key: str = None, note: str = "", extras: str = "[]"):
    if qty < 1:
        return {"error": "Quantity must be at least 1"}

    conn = get_connection()
    cur = get_cursor(conn)

    if idempotency_key:
        cur.execute("SELECT * FROM orders WHERE idempotency_key = %s", (idempotency_key,))
        existing = cur.fetchone()
        if existing:
            cur.close()
            conn.close()
            return format_order(dict(existing))

    cur.execute("SELECT * FROM sessions WHERE session_id = %s", (session_id,))
    session = cur.fetchone()

    if session is None:
        cur.close()
        conn.close()
        return {"error": "Session not found"}

    menu_row = get_menu_item_row(item)
    base_price = menu_row["price"] if menu_row else 0
    available_extras = parse_extras(menu_row["extras"]) if menu_row else []

    try:
        requested_extra_names = json.loads(extras)
        if not isinstance(requested_extra_names, list):
            requested_extra_names = []
    except Exception:
        requested_extra_names = []

    extras_detail = []
    extras_total = 0.0
    for name in requested_extra_names:
        match = next((e for e in available_extras if e.get("name") == name), None)
        if match:
            extras_detail.append(match)
            extras_total += float(match.get("price", 0))

    unit_price = base_price + extras_total

    order_id = f"o_{uuid.uuid4().hex[:8]}"
    cur.execute(
        "INSERT INTO orders (order_id, session_id, item, qty, price, status, idempotency_key, note, extras, ready) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0)",
        (order_id, session_id, item, qty, unit_price, "PENDING_REVIEW", idempotency_key, note, json.dumps(extras_detail)),
    )
    conn.commit()
    cur.close()
    conn.close()

    return {
        "order_id": order_id,
        "session_id": session_id,
        "item": item,
        "qty": qty,
        "price": unit_price,
        "status": "PENDING_REVIEW",
        "note": note,
        "extras": extras_detail,
    }


def send_kitchen_ticket(order_id: str, item: str, qty: int, note: str = "", extras: str = "") -> str:
    for attempt in range(1, 3):
        try:
            response = httpx.post(
                f"{PDA_URL}/kitchen-ticket",
                params={"order_id": order_id, "item": item, "qty": qty, "note": note, "extras": extras},
                timeout=5.0,
            )
            if response.status_code == 200:
                return "SENT"
        except httpx.RequestError as e:
            print(f"⚠ Attempt {attempt}: PDA server unreachable — {e}")

    print(f"❌ Kitchen ticket FAILED after retries for order {order_id}")
    return "FAILED"


@app.post("/orders/{order_id}/approve")
def approve_order(order_id: str, _auth: None = Depends(verify_waiter), waiter_name: str = Depends(get_waiter_name)):
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
    order = cur.fetchone()

    if order is None:
        cur.close()
        conn.close()
        return {"error": "Order not found"}

    if order["status"] != "PENDING_REVIEW":
        cur.close()
        conn.close()
        return {"error": "Order already processed"}

    extras_list = parse_extras(order.get("extras"))
    extras_text = ", ".join(e.get("name", "") for e in extras_list)

    kitchen_status = send_kitchen_ticket(order_id, order["item"], order["qty"], order.get("note") or "", extras_text)

    menu_row = get_menu_item_row(order["item"])
    prep_time = menu_row["prep_time_minutes"] if menu_row and menu_row.get("prep_time_minutes") else 10

    cur.execute("SELECT COUNT(*) as cnt FROM orders WHERE status = 'APPROVED' AND ready = 0")
    queue_ahead = cur.fetchone()["cnt"]

    eta_minutes = prep_time + (queue_ahead * QUEUE_DELAY_MINUTES_PER_ORDER)
    estimated_ready_at = datetime.utcnow() + timedelta(minutes=eta_minutes)

    cur.execute(
        "UPDATE orders SET status = %s, handled_by = %s, estimated_ready_at = %s WHERE order_id = %s",
        ("APPROVED", waiter_name, estimated_ready_at, order_id),
    )
    conn.commit()
    cur.close()
    conn.close()

    log_action(waiter_name, "approved order", order_id)

    return {
        "order_id": order_id,
        "status": "APPROVED",
        "kitchen_status": kitchen_status,
        "estimated_ready_at": estimated_ready_at.isoformat() + "Z",
    }


@app.post("/orders/{order_id}/reject")
def reject_order(order_id: str, _auth: None = Depends(verify_waiter), waiter_name: str = Depends(get_waiter_name)):
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
    order = cur.fetchone()

    if order is None:
        cur.close()
        conn.close()
        return {"error": "Order not found"}

    if order["status"] != "PENDING_REVIEW":
        cur.close()
        conn.close()
        return {"error": "Order already processed"}

    cur.execute("UPDATE orders SET status = %s, handled_by = %s WHERE order_id = %s", ("REJECTED", waiter_name, order_id))
    conn.commit()
    cur.close()
    conn.close()

    log_action(waiter_name, "rejected order", order_id)

    return {"order_id": order_id, "status": "REJECTED"}


@app.post("/orders/{order_id}/ready")
def mark_order_ready(order_id: str):
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
    order = cur.fetchone()

    if order is None:
        cur.close()
        conn.close()
        return {"error": "Order not found"}

    cur.execute("UPDATE orders SET ready = 1 WHERE order_id = %s", (order_id,))
    conn.commit()
    cur.close()
    conn.close()

    return {"order_id": order_id, "ready": True}


@app.post("/orders/{order_id}/picked-up")
def mark_order_picked_up(order_id: str, _auth: None = Depends(verify_waiter), waiter_name: str = Depends(get_waiter_name)):
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
    order = cur.fetchone()

    if order is None:
        cur.close()
        conn.close()
        return {"error": "Order not found"}

    cur.execute("UPDATE orders SET ready = 0 WHERE order_id = %s", (order_id,))
    conn.commit()
    cur.close()
    conn.close()

    log_action(waiter_name, "picked up order", order_id)

    return {"order_id": order_id, "ready": False}


@app.delete("/orders/{order_id}")
def delete_order(order_id: str, _auth: None = Depends(verify_waiter), waiter_name: str = Depends(get_waiter_name)):
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
    order = cur.fetchone()

    if order is None:
        cur.close()
        conn.close()
        return {"error": "Order not found"}

    if order["status"] != "PENDING_REVIEW":
        cur.close()
        conn.close()
        return {"error": "Order already processed"}

    cur.execute("DELETE FROM orders WHERE order_id = %s", (order_id,))
    conn.commit()
    cur.close()
    conn.close()

    log_action(waiter_name, "deleted order", order_id)

    return {"order_id": order_id, "deleted": True}