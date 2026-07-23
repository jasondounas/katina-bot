# KatinaBot — QR Restaurant Ordering System

A full restaurant ordering system built with FastAPI, designed around QR-code table sessions, waiter approval workflows, and real-time kitchen tickets. Built as a project for Softbiz, deployed on Render.

## What it does
- Customers scan a QR code at their table, view the menu, order, and track order status live
- Waiters see a live dashboard of all tables, approve/reject incoming orders, calculate bill splits, and manage table sessions
- Kitchen staff see a live ticket board of approved orders and mark them done as they're prepared

## Files
- **`main.py`** — Main API: manages tables, sessions, and orders (FastAPI + PostgreSQL via `psycopg2`). Handles the full order lifecycle (`PENDING_REVIEW → APPROVED/REJECTED`), session state (party size, paid status, waiter-called/payment-requested flags), and calls the PDA service to generate kitchen tickets on order approval, with retry logic if that service is temporarily unreachable.
- **`pda_server.py`** — A separate, lightweight FastAPI service that owns the menu and kitchen ticket queue, deployed independently so the kitchen display can poll it directly.
- **`customer.html`** / **`waiter.html`** / **`kitchen.html`** — Vanilla HTML/CSS/JS interfaces for each role, polling the relevant API on an interval (no build step, no framework).
- **`check_db.py`** — Dev utility from an earlier local-SQLite prototyping stage, before the project moved to PostgreSQL for deployment.

## Tech Stack
- **Backend:** Python, FastAPI, PostgreSQL (`psycopg2`), `httpx` for service-to-service calls
- **Frontend:** Vanilla HTML/CSS/JS
- **Deployment:** Render (main API and PDA/kitchen service deployed as two independent services)

## Running locally
```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://user:pass@localhost:5432/katina
uvicorn main:app --reload --port 8000

# in a second terminal, for the kitchen ticket service
uvicorn pda_server:app --reload --port 8001
```
Then open any of the `.html` files in a browser (update the `BASE`/`PDA` URLs near the top of each file to point at your local servers if not using the deployed ones).

## Notes
Built solo as a real, end-to-end system — covers cross-service communication with failure handling, session/state management, and role-specific interfaces for customers, waiters, and kitchen staff.
