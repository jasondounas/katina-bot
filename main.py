from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uuid
import os
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


def get_connection():
    conn = psycopg2.connect(DATABASE_URL)
    return conn


def get_cursor(conn):
    # returns rows as dict-like objects, same convenience as sqlite3.Row gave us
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


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
    conn.commit()

    # Migration: add any columns missing from an older version of "sessions"
    for column_def in ["waiter_called INTEGER DEFAULT 0", "payment_requested INTEGER DEFAULT 0"]:
        try:
            cur.execute(f"ALTER TABLE sessions ADD COLUMN {column_def}")
            conn.commit()
        except psycopg2.errors.DuplicateColumn:
            conn.rollback()  # Postgres requires a rollback after a failed statement

    cur.close()
    conn.close()


init_db()


@app.get("/")
def root():
    return {"status": "alive"}


@app.get("/menu")
def get_menu():
    try:
        response = httpx.get(f"{PDA_URL}/menu", timeout=5.0)
        return response.json()
    except httpx.RequestError:
        return {}


# ---------- Tables (permanent) ----------

@app.post("/tables")
def create_table(table_id: str, display_label: str = None):
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
def release_table(table_id: str):
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
    return {"table_id": table_id, "released": True}


# ---------- Sessions ----------

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
def open_session(table_id: str, party_size: int):
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

    return {
        "session_id": session_id,
        "table_id": table_id,
        "party_size": party_size,
        "status": "OPEN",
        "is_paid": False,
    }


@app.post("/sessions/{session_id}/update-party-size")
def update_party_size(session_id: str, party_size: int):
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

    return {"session_id": session_id, "party_size": party_size}


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str):
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

    return {"session_id": session_id, "status": "DELETED"}


@app.get("/sessions/{session_id}/orders")
def get_session_orders(session_id: str):
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM orders WHERE session_id = %s", (session_id,))
    orders = cur.fetchall()
    cur.close()
    conn.close()

    return [dict(order) for order in orders]


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
def mark_paid(session_id: str):
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM sessions WHERE session_id = %s", (session_id,))
    session = cur.fetchone()

    if session is None:
        cur.close()
        conn.close()
        return {"error": "Session not found"}

    cur.execute(
        "UPDATE sessions SET is_paid = 1, payment_requested = 0 WHERE session_id = %s",
        (session_id,),
    )
    conn.commit()
    cur.close()
    conn.close()

    return {"session_id": session_id, "is_paid": True}


@app.post("/sessions/{session_id}/close")
def close_session(session_id: str):
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

    if not session["is_paid"]:
        cur.close()
        conn.close()
        return {"error": "Cannot close, payment not received"}

    cur.execute("UPDATE sessions SET status = %s WHERE session_id = %s", ("CLOSED", session_id))
    conn.commit()
    cur.close()
    conn.close()

    return {"session_id": session_id, "status": "CLOSED"}


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
def acknowledge_call(session_id: str):
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("UPDATE sessions SET waiter_called = 0 WHERE session_id = %s", (session_id,))
    conn.commit()
    cur.close()
    conn.close()
    return {"session_id": session_id, "waiter_called": False}


@app.post("/sessions/{session_id}/request-payment")
def request_payment(session_id: str):
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM sessions WHERE session_id = %s", (session_id,))
    if cur.fetchone() is None:
        cur.close()
        conn.close()
        return {"error": "Session not found"}

    cur.execute("UPDATE sessions SET payment_requested = 1 WHERE session_id = %s", (session_id,))
    conn.commit()
    cur.close()
    conn.close()
    return {"session_id": session_id, "payment_requested": True}


# ---------- Orders ----------

@app.get("/orders/pending")
def get_pending_orders():
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM orders WHERE status = 'PENDING_REVIEW'")
    orders = cur.fetchall()
    cur.close()
    conn.close()

    return [dict(order) for order in orders]


def get_price_from_pda(item: str) -> float:
    try:
        response = httpx.get(f"{PDA_URL}/menu", timeout=5.0)
        menu = response.json()
        return menu.get(item, 0)
    except httpx.RequestError as e:
        print(f"⚠ PDA unreachable while fetching menu — {e}")
        return 0


@app.post("/orders")
def submit_order(session_id: str, item: str, qty: int):
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM sessions WHERE session_id = %s", (session_id,))
    session = cur.fetchone()

    if session is None:
        cur.close()
        conn.close()
        return {"error": "Session not found"}

    price = get_price_from_pda(item)

    order_id = f"o_{uuid.uuid4().hex[:8]}"
    cur.execute(
        "INSERT INTO orders (order_id, session_id, item, qty, price, status) VALUES (%s, %s, %s, %s, %s, %s)",
        (order_id, session_id, item, qty, price, "PENDING_REVIEW"),
    )
    conn.commit()
    cur.close()
    conn.close()

    return {
        "order_id": order_id,
        "session_id": session_id,
        "item": item,
        "qty": qty,
        "price": price,
        "status": "PENDING_REVIEW",
    }


def send_kitchen_ticket(order_id: str, item: str, qty: int) -> str:
    for attempt in range(1, 3):
        try:
            response = httpx.post(
                f"{PDA_URL}/kitchen-ticket",
                params={"order_id": order_id, "item": item, "qty": qty},
                timeout=5.0,
            )
            if response.status_code == 200:
                return "SENT"
        except httpx.RequestError as e:
            print(f"⚠ Attempt {attempt}: PDA server unreachable — {e}")

    print(f"❌ Kitchen ticket FAILED after retries for order {order_id}")
    return "FAILED"


@app.post("/orders/{order_id}/approve")
def approve_order(order_id: str):
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

    kitchen_status = send_kitchen_ticket(order_id, order["item"], order["qty"])

    cur.execute("UPDATE orders SET status = %s WHERE order_id = %s", ("APPROVED", order_id))
    conn.commit()
    cur.close()
    conn.close()

    return {
        "order_id": order_id,
        "status": "APPROVED",
        "kitchen_status": kitchen_status,
    }


@app.post("/orders/{order_id}/reject")
def reject_order(order_id: str):
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

    cur.execute("UPDATE orders SET status = %s WHERE order_id = %s", ("REJECTED", order_id))
    conn.commit()
    cur.close()
    conn.close()

    return {"order_id": order_id, "status": "REJECTED"}


@app.delete("/orders/{order_id}")
def delete_order(order_id: str):
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

    return {"order_id": order_id, "deleted": True}