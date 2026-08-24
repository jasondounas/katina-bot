from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

kitchen_tickets = []


@app.get("/")
def root():
    return {"status": "alive"}


@app.post("/kitchen-ticket")
def kitchen_ticket(order_id: str, item: str, qty: int, note: str = "", extras: str = "", table_label: str = ""):
    print(f"🧾 KITCHEN TICKET — {table_label or order_id}: {qty}x {item}" + (f" (extras: {extras})" if extras else "") + (f" (note: {note})" if note else ""))
    kitchen_tickets.append({"order_id": order_id, "item": item, "qty": qty, "note": note, "extras": extras, "table_label": table_label, "done": False})
    return {"success": True}


@app.get("/kitchen-tickets")
def get_kitchen_tickets():
    return [t for t in kitchen_tickets if not t["done"]]


@app.post("/kitchen-tickets/{order_id}/done")
def mark_ticket_done(order_id: str):
    for t in kitchen_tickets:
        if t["order_id"] == order_id:
            t["done"] = True
            return {"success": True}
    return {"error": "Ticket not found"}