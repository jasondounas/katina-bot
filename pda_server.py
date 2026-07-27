from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

MENU = {
    "coke": {"price": 3.50, "image": "https://katina-bot-2.onrender.com/image/coke.jpg"},
    "burger": {"price": 9.90, "image": "https://katina-bot-2.onrender.com/image/burger.jpg"},
    "salad": {"price": 7.20, "image": "https://katina-bot-2.onrender.com/image/salad.jpg"},
}

kitchen_tickets = []


@app.get("/menu")
def get_menu():
    return MENU


@app.post("/kitchen-ticket")
def kitchen_ticket(order_id: str, item: str, qty: int):
    print(f"🧾 KITCHEN TICKET — Order {order_id}: {qty}x {item}")
    kitchen_tickets.append({"order_id": order_id, "item": item, "qty": qty, "done": False})
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