"""
REST Simulator — Configurable mock CRUD API.

Run standalone:
    python -m rest_simulator.server

Endpoints:
    GET    /items              — List all items
    GET    /items/{id}         — Get item by ID
    POST   /items              — Create item
    PUT    /items/{id}         — Update item
    DELETE /items/{id}         — Delete item

    GET    /config             — View current error overrides
    POST   /config             — Set error overrides per method/path
    DELETE /config             — Clear all overrides

Override format (POST /config):
    {
        "GET /items":       {"status": 500, "body": {"error": "Internal Server Error"}},
        "GET /items/1":     {"status": 401, "body": {"error": "Unauthorized"}},
        "POST /items":      {"status": 201, "body": {"message": "Created (override)"}},
        "DELETE /items/3":  {"status": 403, "body": {"error": "Forbidden"}}
    }

    Key format: "<METHOD> <path>"
    Use "*" as wildcard ID: "GET /items/*" matches any GET /items/{id}
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger("rest-simulator")

app = FastAPI(title="REST Simulator", docs_url="/docs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------

_items: dict[str, dict[str, Any]] = {}
_overrides: dict[str, dict[str, Any]] = {}
_request_log: list[dict] = []
MAX_LOG = 200


def _seed_items():
    """Seed some sample items."""
    samples = [
        {"name": "Laptop", "price": 999.99, "category": "electronics"},
        {"name": "Headphones", "price": 49.99, "category": "electronics"},
        {"name": "Coffee Mug", "price": 12.50, "category": "kitchen"},
    ]
    for s in samples:
        item_id = str(uuid.uuid4())[:8]
        _items[item_id] = {
            "id": item_id,
            **s,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }


_seed_items()


# ---------------------------------------------------------------------------
# Override engine
# ---------------------------------------------------------------------------

def _check_override(method: str, path: str, item_id: str | None = None) -> JSONResponse | None:
    """Check if there's a configured override for this request.
    Matches in order: exact path → wildcard ID → method-only."""

    candidates = [f"{method} {path}"]
    if item_id:
        candidates.append(f"{method} /items/*")
    candidates.append(f"{method} /items")

    for key in candidates:
        if key in _overrides:
            cfg = _overrides[key]
            status = cfg.get("status", 200)
            body = cfg.get("body", {"error": f"Simulated {status}"})
            delay = cfg.get("delay_ms", 0)
            if delay:
                import time
                time.sleep(delay / 1000)
            logger.info("Override matched [%s]: returning %d", key, status)
            return JSONResponse(status_code=status, content=body)
    return None


def _log_request(method: str, path: str, status: int, overridden: bool):
    _request_log.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "method": method,
        "path": path,
        "status": status,
        "overridden": overridden,
    })
    if len(_request_log) > MAX_LOG:
        _request_log.pop(0)


# ---------------------------------------------------------------------------
# CRUD — Items
# ---------------------------------------------------------------------------

@app.get("/items")
def list_items(request: Request):
    override = _check_override("GET", "/items")
    if override:
        _log_request("GET", "/items", override.status_code, True)
        return override
    _log_request("GET", "/items", 200, False)
    return {"items": list(_items.values()), "total": len(_items)}


@app.get("/items/{item_id}")
def get_item(item_id: str, request: Request):
    override = _check_override("GET", f"/items/{item_id}", item_id)
    if override:
        _log_request("GET", f"/items/{item_id}", override.status_code, True)
        return override
    if item_id not in _items:
        _log_request("GET", f"/items/{item_id}", 404, False)
        raise HTTPException(404, detail="Item not found")
    _log_request("GET", f"/items/{item_id}", 200, False)
    return _items[item_id]


@app.post("/items", status_code=201)
async def create_item(request: Request):
    override = _check_override("POST", "/items")
    if override:
        _log_request("POST", "/items", override.status_code, True)
        return override
    body = await request.json()
    item_id = str(uuid.uuid4())[:8]
    item = {
        "id": item_id,
        "name": body.get("name", "Unnamed"),
        "price": body.get("price", 0),
        "category": body.get("category", "general"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _items[item_id] = item
    _log_request("POST", "/items", 201, False)
    return item


@app.put("/items/{item_id}")
async def update_item(item_id: str, request: Request):
    override = _check_override("PUT", f"/items/{item_id}", item_id)
    if override:
        _log_request("PUT", f"/items/{item_id}", override.status_code, True)
        return override
    if item_id not in _items:
        _log_request("PUT", f"/items/{item_id}", 404, False)
        raise HTTPException(404, detail="Item not found")
    body = await request.json()
    _items[item_id].update({
        k: v for k, v in body.items() if k != "id"
    })
    _items[item_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
    _log_request("PUT", f"/items/{item_id}", 200, False)
    return _items[item_id]


@app.delete("/items/{item_id}")
def delete_item(item_id: str, request: Request):
    override = _check_override("DELETE", f"/items/{item_id}", item_id)
    if override:
        _log_request("DELETE", f"/items/{item_id}", override.status_code, True)
        return override
    if item_id not in _items:
        _log_request("DELETE", f"/items/{item_id}", 404, False)
        raise HTTPException(404, detail="Item not found")
    del _items[item_id]
    _log_request("DELETE", f"/items/{item_id}", 204, False)
    return JSONResponse(status_code=204, content=None)


# ---------------------------------------------------------------------------
# Override config API
# ---------------------------------------------------------------------------

@app.get("/config")
def get_config():
    return {"overrides": _overrides, "total": len(_overrides)}


@app.post("/config")
async def set_config(request: Request):
    """Merge overrides. POST body is a dict of 'METHOD /path' → {status, body, delay_ms}."""
    body = await request.json()
    _overrides.update(body)
    return {"overrides": _overrides, "message": f"Set {len(body)} override(s)"}


@app.delete("/config")
def clear_config():
    _overrides.clear()
    return {"message": "All overrides cleared"}


@app.delete("/config/{key:path}")
def delete_config_key(key: str):
    if key in _overrides:
        del _overrides[key]
        return {"message": f"Override '{key}' removed"}
    raise HTTPException(404, detail=f"Override '{key}' not found")


# ---------------------------------------------------------------------------
# Request log
# ---------------------------------------------------------------------------

@app.get("/log")
def get_log(limit: int = 50):
    return {"log": _request_log[-limit:], "total": len(_request_log)}


@app.delete("/log")
def clear_log():
    _request_log.clear()
    return {"message": "Log cleared"}


# ---------------------------------------------------------------------------
# Reset — wipe items + overrides + log, re-seed
# ---------------------------------------------------------------------------

@app.post("/reset")
def reset():
    _items.clear()
    _overrides.clear()
    _request_log.clear()
    _seed_items()
    return {"message": "Reset complete — items re-seeded, overrides and log cleared"}


# ---------------------------------------------------------------------------
# Dashboard UI
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard():
    return _DASHBOARD_HTML


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>REST Simulator</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>body { font-family: 'Inter', system-ui, sans-serif; }</style>
</head>
<body class="bg-gray-900 text-gray-100 min-h-screen">
<div class="max-w-6xl mx-auto px-4 py-6">

    <!-- Header -->
    <div class="flex items-center justify-between mb-6">
        <h1 class="text-2xl font-bold text-white">REST Simulator</h1>
        <div class="flex gap-2">
            <button onclick="resetAll()" class="px-3 py-1.5 bg-red-600 hover:bg-red-700 rounded text-sm font-medium">Reset All</button>
            <a href="/docs" class="px-3 py-1.5 bg-gray-700 hover:bg-gray-600 rounded text-sm font-medium">Swagger Docs</a>
        </div>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">

        <!-- Left: Overrides -->
        <div>
            <h2 class="text-lg font-semibold mb-3 text-yellow-400">Error Overrides</h2>

            <!-- Quick presets -->
            <div class="flex flex-wrap gap-2 mb-4">
                <button onclick="setPreset('GET /items', 500, 'Internal Server Error')"
                        class="px-2 py-1 bg-red-800 hover:bg-red-700 rounded text-xs">GET /items → 500</button>
                <button onclick="setPreset('GET /items/*', 401, 'Unauthorized')"
                        class="px-2 py-1 bg-orange-800 hover:bg-orange-700 rounded text-xs">GET /items/* → 401</button>
                <button onclick="setPreset('POST /items', 429, 'Too Many Requests')"
                        class="px-2 py-1 bg-yellow-800 hover:bg-yellow-700 rounded text-xs">POST → 429</button>
                <button onclick="setPreset('PUT /items/*', 403, 'Forbidden')"
                        class="px-2 py-1 bg-purple-800 hover:bg-purple-700 rounded text-xs">PUT → 403</button>
                <button onclick="setPreset('DELETE /items/*', 503, 'Service Unavailable')"
                        class="px-2 py-1 bg-pink-800 hover:bg-pink-700 rounded text-xs">DELETE → 503</button>
                <button onclick="clearOverrides()"
                        class="px-2 py-1 bg-green-800 hover:bg-green-700 rounded text-xs">Clear All</button>
            </div>

            <!-- Custom override form -->
            <div class="bg-gray-800 rounded-lg p-4 mb-4">
                <p class="text-xs text-gray-400 mb-2">Custom Override</p>
                <div class="flex gap-2 mb-2">
                    <select id="ovr-method" class="bg-gray-700 rounded px-2 py-1 text-sm flex-shrink-0">
                        <option>GET</option><option>POST</option><option>PUT</option><option>DELETE</option>
                    </select>
                    <input id="ovr-path" class="bg-gray-700 rounded px-2 py-1 text-sm flex-1" placeholder="/items  or  /items/*" value="/items">
                    <input id="ovr-status" type="number" class="bg-gray-700 rounded px-2 py-1 text-sm w-20" placeholder="500" value="500">
                </div>
                <div class="flex gap-2">
                    <input id="ovr-body" class="bg-gray-700 rounded px-2 py-1 text-sm flex-1" placeholder='{"error":"message"}' value='{"error":"Internal Server Error"}'>
                    <input id="ovr-delay" type="number" class="bg-gray-700 rounded px-2 py-1 text-sm w-24" placeholder="delay ms" value="0">
                    <button onclick="addOverride()" class="px-3 py-1 bg-indigo-600 hover:bg-indigo-700 rounded text-sm font-medium">Add</button>
                </div>
            </div>

            <!-- Active overrides -->
            <div id="overrides-list" class="space-y-2"></div>
        </div>

        <!-- Right: Items + Log -->
        <div>
            <h2 class="text-lg font-semibold mb-3 text-green-400">Items (Live Data)</h2>
            <div id="items-list" class="bg-gray-800 rounded-lg p-4 mb-6 max-h-60 overflow-y-auto text-sm font-mono"></div>

            <h2 class="text-lg font-semibold mb-3 text-blue-400">Request Log</h2>
            <div id="log-list" class="bg-gray-800 rounded-lg p-4 max-h-72 overflow-y-auto text-xs font-mono"></div>
        </div>
    </div>
</div>

<script>
const BASE = '';

async function loadOverrides() {
    const r = await fetch(`${BASE}/config`);
    const d = await r.json();
    const el = document.getElementById('overrides-list');
    if (!Object.keys(d.overrides).length) { el.innerHTML = '<p class="text-gray-500 text-sm">No overrides active — all endpoints return normal responses.</p>'; return; }
    el.innerHTML = Object.entries(d.overrides).map(([k, v]) => `
        <div class="flex items-center justify-between bg-gray-800 rounded px-3 py-2">
            <div>
                <span class="font-mono text-yellow-300 text-sm">${k}</span>
                <span class="ml-2 px-1.5 py-0.5 rounded text-xs font-bold ${statusColor(v.status)}">${v.status}</span>
                ${v.delay_ms ? `<span class="ml-1 text-gray-400 text-xs">+${v.delay_ms}ms</span>` : ''}
            </div>
            <button onclick="removeOverride('${k}')" class="text-red-400 hover:text-red-300 text-xs">remove</button>
        </div>
    `).join('');
}

async function loadItems() {
    try {
        const r = await fetch(`${BASE}/items`);
        const d = await r.json();
        const el = document.getElementById('items-list');
        if (!d.items || !d.items.length) { el.innerHTML = '<p class="text-gray-500">No items.</p>'; return; }
        el.innerHTML = d.items.map(i => `<div class="mb-1"><span class="text-green-300">${i.id}</span> ${i.name} — $${i.price} <span class="text-gray-500">[${i.category}]</span></div>`).join('');
    } catch(e) { document.getElementById('items-list').innerHTML = `<p class="text-red-400">Error fetching items</p>`; }
}

async function loadLog() {
    const r = await fetch(`${BASE}/log?limit=30`);
    const d = await r.json();
    const el = document.getElementById('log-list');
    if (!d.log.length) { el.innerHTML = '<p class="text-gray-500">No requests yet.</p>'; return; }
    el.innerHTML = d.log.reverse().map(l => `
        <div class="mb-0.5">
            <span class="text-gray-500">${l.timestamp.split('T')[1].split('.')[0]}</span>
            <span class="font-bold ${methodColor(l.method)}">${l.method.padEnd(6)}</span>
            <span class="text-gray-300">${l.path}</span>
            <span class="ml-1 ${statusColor(l.status)} px-1 rounded text-xs font-bold">${l.status}</span>
            ${l.overridden ? '<span class="text-yellow-500 text-xs ml-1">overridden</span>' : ''}
        </div>
    `).join('');
}

function statusColor(s) {
    if (s >= 500) return 'bg-red-700 text-red-100';
    if (s >= 400) return 'bg-orange-700 text-orange-100';
    if (s >= 300) return 'bg-blue-700 text-blue-100';
    if (s >= 200) return 'bg-green-700 text-green-100';
    return 'bg-gray-600';
}
function methodColor(m) {
    return {GET:'text-green-400',POST:'text-blue-400',PUT:'text-yellow-400',DELETE:'text-red-400'}[m] || 'text-gray-400';
}

async function setPreset(key, status, msg) {
    await fetch(`${BASE}/config`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({[key]: {status, body:{error:msg}}})});
    refresh();
}
async function addOverride() {
    const method = document.getElementById('ovr-method').value;
    const path = document.getElementById('ovr-path').value;
    const status = parseInt(document.getElementById('ovr-status').value);
    const delay = parseInt(document.getElementById('ovr-delay').value) || 0;
    let body;
    try { body = JSON.parse(document.getElementById('ovr-body').value); } catch { body = {error: document.getElementById('ovr-body').value}; }
    const key = `${method} ${path}`;
    const payload = {status, body};
    if (delay) payload.delay_ms = delay;
    await fetch(`${BASE}/config`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({[key]: payload})});
    refresh();
}
async function removeOverride(key) {
    await fetch(`${BASE}/config/${key}`, {method:'DELETE'});
    refresh();
}
async function clearOverrides() {
    await fetch(`${BASE}/config`, {method:'DELETE'});
    refresh();
}
async function resetAll() {
    await fetch(`${BASE}/reset`, {method:'POST'});
    refresh();
}

function refresh() { loadOverrides(); loadItems(); loadLog(); }
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

PORT = 8081

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("Starting REST Simulator on port %d...", PORT)
    logger.info("Dashboard: http://localhost:%d/", PORT)
    logger.info("Swagger:   http://localhost:%d/docs", PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
