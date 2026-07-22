from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import uuid
import sqlite3
import httpx

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

PDA_URL = "https://katina-bot-1.onrender.com"


def get_connection():
    conn = sqlite3.connect("katina.db")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            table_id TEXT,
            party_size INTEGER,
            status TEXT,
            is_paid INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
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


# ---------- Sessions ----------

@app.get("/sessions")
def list_sessions(status: str = "OPEN"):
    conn = get_connection()
    cursor = conn.execute("SELECT * FROM sessions WHERE status = ?", (status,))
    sessions = cursor.fetchall()
    conn.close()

    return [dict(s) for s in sessions]


@app.post("/sessions/open")
def open_session(table_id: str, party_size: int):
    session_id = f"s_{uuid.uuid4().hex[:8]}"

    conn = get_connection()
    conn.execute(
        "INSERT INTO sessions (session_id, table_id, party_size, status, is_paid) VALUES (?, ?, ?, ?, ?)",
        (session_id, table_id, party_size, "OPEN", 0),
    )
    conn.commit()
    conn.close()

    return {
        "session_id": session_id,
        "table_id": table_id,
        "party_size": party_size,
        "status": "OPEN",
        "is_paid": False,
    }


@app.get("/tables/{table_id}/active-session")
def get_active_session(table_id: str):
    conn = get_connection()
    cursor = conn.execute(
        "SELECT * FROM sessions WHERE table_id = ? AND status = 'OPEN' ORDER BY rowid DESC LIMIT 1",
        (table_id,),
    )
    session = cursor.fetchone()
    conn.close()

    if session is None:
        return {"active": False}
    return {"active": True, "session_id": session["session_id"]}


@app.post("/sessions/{session_id}/update-party-size")
def update_party_size(session_id: str, party_size: int):
    conn = get_connection()
    cursor = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
    session = cursor.fetchone()

    if session is None:
        conn.close()
        return {"error": "Session not found"}

    if session["status"] != "OPEN":
        conn.close()
        return {"error": "Cannot update party size on a closed session"}

    conn.execute(
        "UPDATE sessions SET party_size = ? WHERE session_id = ?",
        (party_size, session_id),
    )
    conn.commit()
    conn.close()

    return {"session_id": session_id, "party_size": party_size}


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    conn = get_connection()
    cursor = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
    session = cursor.fetchone()

    if session is None:
        conn.close()
        return {"error": "Session not found"}

    if session["status"] != "OPEN":
        conn.close()
        return {"error": "Session already closed or deleted"}

    conn.execute("UPDATE sessions SET status = ? WHERE session_id = ?", ("DELETED", session_id))
    conn.execute(
        "UPDATE orders SET status = 'CANCELLED' WHERE session_id = ? AND status != 'CANCELLED'",
        (session_id,),
    )

    conn.commit()
    conn.close()

    return {"session_id": session_id, "status": "DELETED"}


@app.get("/sessions/{session_id}/orders")
def get_session_orders(session_id: str):
    conn = get_connection()
    cursor = conn.execute("SELECT * FROM orders WHERE session_id = ?", (session_id,))
    orders = cursor.fetchall()
    conn.close()

    return [dict(order) for order in orders]


@app.post("/sessions/{session_id}/split")
def calculate_split(session_id: str):
    conn = get_connection()

    cursor = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
    session = cursor.fetchone()

    if session is None:
        conn.close()
        return {"error": "Session not found"}

    cursor = conn.execute(
        "SELECT * FROM orders WHERE session_id = ? AND status = 'APPROVED'",
        (session_id,),
    )
    approved_orders = cursor.fetchall()
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
    cursor = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
    session = cursor.fetchone()

    if session is None:
        conn.close()
        return {"error": "Session not found"}

    conn.execute("UPDATE sessions SET is_paid = 1 WHERE session_id = ?", (session_id,))
    conn.commit()
    conn.close()

    return {"session_id": session_id, "is_paid": True}


@app.post("/sessions/{session_id}/close")
def close_session(session_id: str):
    conn = get_connection()
    cursor = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
    session = cursor.fetchone()

    if session is None:
        conn.close()
        return {"error": "Session not found"}

    if session["status"] != "OPEN":
        conn.close()
        return {"error": "Session is not open"}

    if not session["is_paid"]:
        conn.close()
        return {"error": "Cannot close, payment not received"}

    conn.execute("UPDATE sessions SET status = ? WHERE session_id = ?", ("CLOSED", session_id))
    conn.commit()
    conn.close()

    return {"session_id": session_id, "status": "CLOSED"}


# ---------- Orders ----------

@app.get("/orders/pending")
def get_pending_orders():
    conn = get_connection()
    cursor = conn.execute("SELECT * FROM orders WHERE status = 'PENDING_REVIEW'")
    orders = cursor.fetchall()
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
    cursor = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
    session = cursor.fetchone()

    if session is None:
        conn.close()
        return {"error": "Session not found"}

    price = get_price_from_pda(item)

    order_id = f"o_{uuid.uuid4().hex[:8]}"
    conn.execute(
        "INSERT INTO orders (order_id, session_id, item, qty, price, status) VALUES (?, ?, ?, ?, ?, ?)",
        (order_id, session_id, item, qty, price, "PENDING_REVIEW"),
    )
    conn.commit()
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
    cursor = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
    order = cursor.fetchone()

    if order is None:
        conn.close()
        return {"error": "Order not found"}

    if order["status"] != "PENDING_REVIEW":
        conn.close()
        return {"error": "Order already processed"}

    kitchen_status = send_kitchen_ticket(order_id, order["item"], order["qty"])

    conn.execute("UPDATE orders SET status = ? WHERE order_id = ?", ("APPROVED", order_id))
    conn.commit()
    conn.close()

    return {
        "order_id": order_id,
        "status": "APPROVED",
        "kitchen_status": kitchen_status,
    }


@app.post("/orders/{order_id}/reject")
def reject_order(order_id: str):
    conn = get_connection()
    cursor = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
    order = cursor.fetchone()

    if order is None:
        conn.close()
        return {"error": "Order not found"}

    if order["status"] != "PENDING_REVIEW":
        conn.close()
        return {"error": "Order already processed"}

    conn.execute("UPDATE orders SET status = ? WHERE order_id = ?", ("REJECTED", order_id))
    conn.commit()
    conn.close()

    return {"order_id": order_id, "status": "REJECTED"}


@app.delete("/orders/{order_id}")
def delete_order(order_id: str):
    conn = get_connection()
    cursor = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
    order = cursor.fetchone()

    if order is None:
        conn.close()
        return {"error": "Order not found"}

    if order["status"] != "PENDING_REVIEW":
        conn.close()
        return {"error": "Order already processed"}

    conn.execute("DELETE FROM orders WHERE order_id = ?", (order_id,))
    conn.commit()
    conn.close()

    return {"order_id": order_id, "deleted": True}


# ---------- Tables ----------

@app.post("/tables/{table_id}/release")
def release_table(table_id: str):
    conn = get_connection()
    cursor = conn.execute(
        "SELECT * FROM sessions WHERE table_id = ? ORDER BY rowid DESC LIMIT 1",
        (table_id,),
    )
    session = cursor.fetchone()

    if session is None:
        conn.close()
        return {"error": "No session found for this table"}

    if session["status"] not in ("CLOSED", "DELETED"):
        conn.close()
        return {"error": "Session must be closed before releasing the table"}

    conn.close()
    return {"table_id": table_id, "released": True}