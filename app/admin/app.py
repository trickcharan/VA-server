"""
FastAPI Admin UI — agent config management with org-scoped auth.

Routes:
  /admin/login         — login page
  /admin/logout        — logout
  /admin/              — dashboard (agents list for org-admin, orgs list for super-admin)
  /admin/agents/...    — agent CRUD
  /admin/orgs/...      — org + user management (super-admin only)
"""

import json
import logging
from functools import wraps
from typing import Annotated

from fastapi import FastAPI, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db.database import get_db, init_db
from app.db.models import Org, User, Agent, UserRole
from app.db.auth import (
    verify_password, hash_password,
    create_session_token, decode_session_token,
)
from app.db.seed import seed

import os

logger = logging.getLogger("admin")

app = FastAPI(title="VA Admin", docs_url=None, redoc_url=None)

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=_TEMPLATE_DIR)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
def on_startup():
    init_db()
    # Auto-seed on first run
    from app.db.database import SessionLocal
    db = SessionLocal()
    try:
        user_count = db.query(User).count()
        if user_count == 0:
            logger.info("No users found — running seed...")
            seed()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

SESSION_COOKIE = "va_session"


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    payload = decode_session_token(token)
    if not payload:
        return None
    user = db.query(User).filter(User.id == payload["uid"], User.is_active == True).first()
    return user


def require_login(request: Request, db: Session = Depends(get_db)) -> User:
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})
    return user


def require_super_admin(request: Request, db: Session = Depends(get_db)) -> User:
    user = require_login(request, db)
    if user.role != UserRole.super_admin:
        raise HTTPException(status_code=403, detail="Super-admin access required")
    return user


# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------

@app.get("/admin/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None})


@app.post("/admin/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == username, User.is_active == True).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "Invalid username or password"}, status_code=401
        )
    token = create_session_token(user.id)
    response = RedirectResponse(url="/admin/", status_code=303)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, max_age=60 * 60 * 24)
    return response


@app.get("/admin/logout")
def logout():
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/admin/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(require_login)):
    if user.role == UserRole.super_admin:
        orgs = db.query(Org).order_by(Org.id).all()
        return templates.TemplateResponse(request=request, name="super_dashboard.html", context={
            "user": user, "orgs": orgs,
        })
    else:
        agents = db.query(Agent).filter(Agent.org_id == user.org_id).order_by(Agent.id).all()
        org = db.query(Org).filter(Org.id == user.org_id).first()
        return templates.TemplateResponse(request=request, name="org_dashboard.html", context={
            "user": user, "org": org, "agents": agents,
        })


# ---------------------------------------------------------------------------
# Agent CRUD (org-admin)
# ---------------------------------------------------------------------------

@app.get("/admin/agents/new", response_class=HTMLResponse)
def agent_new(request: Request, db: Session = Depends(get_db), user: User = Depends(require_login)):
    return templates.TemplateResponse(request=request, name="agent_form.html", context={
        "user": user, "agent": None, "error": None,
    })


@app.post("/admin/agents/new")
def agent_create(
    request: Request,
    name: str = Form(...),
    provider: str = Form("google_live"),
    system_instruction: str = Form(""),
    context: str = Form(""),
    tools_json: str = Form("[]"),
    api_base_url: str = Form(""),
    api_docs: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_login),
):
    # Validate tools JSON
    try:
        tools = json.loads(tools_json)
        if not isinstance(tools, list):
            raise ValueError("Tools must be a JSON array")
    except (json.JSONDecodeError, ValueError) as e:
        return templates.TemplateResponse(request=request, name="agent_form.html", context={
            "user": user, "agent": None,
            "error": f"Invalid tools JSON: {e}",
        }, status_code=400)

    org_id = user.org_id
    # Super-admin creating agent needs an org_id from query param
    if user.role == UserRole.super_admin:
        org_id = request.query_params.get("org_id")
        if not org_id:
            raise HTTPException(400, "org_id required for super-admin")
        org_id = int(org_id)

    agent = Agent(
        org_id=org_id,
        name=name,
        provider=provider,
        system_instruction=system_instruction,
        context=context,
        tools=tools,
        api_base_url=api_base_url.strip() or None,
        api_docs=api_docs,
    )
    db.add(agent)
    db.commit()
    return RedirectResponse(url="/admin/", status_code=303)


@app.get("/admin/agents/{agent_id}/edit", response_class=HTMLResponse)
def agent_edit(
    agent_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_login),
):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(404, "Agent not found")
    # Org-admin can only edit their own org's agents
    if user.role == UserRole.org_admin and agent.org_id != user.org_id:
        raise HTTPException(403, "Access denied")
    return templates.TemplateResponse(request=request, name="agent_form.html", context={
        "user": user, "agent": agent, "error": None,
    })


@app.post("/admin/agents/{agent_id}/edit")
def agent_update(
    agent_id: int,
    request: Request,
    name: str = Form(...),
    provider: str = Form("google_live"),
    system_instruction: str = Form(""),
    context: str = Form(""),
    tools_json: str = Form("[]"),
    api_base_url: str = Form(""),
    api_docs: str = Form(""),
    is_active: bool = Form(True),
    db: Session = Depends(get_db),
    user: User = Depends(require_login),
):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(404, "Agent not found")
    if user.role == UserRole.org_admin and agent.org_id != user.org_id:
        raise HTTPException(403, "Access denied")

    try:
        tools = json.loads(tools_json)
        if not isinstance(tools, list):
            raise ValueError("Tools must be a JSON array")
    except (json.JSONDecodeError, ValueError) as e:
        return templates.TemplateResponse(request=request, name="agent_form.html", context={
            "user": user, "agent": agent,
            "error": f"Invalid tools JSON: {e}",
        }, status_code=400)

    agent.name = name
    agent.provider = provider
    agent.system_instruction = system_instruction
    agent.context = context
    agent.tools = tools
    agent.api_base_url = api_base_url.strip() or None
    agent.api_docs = api_docs
    agent.is_active = is_active
    db.commit()
    return RedirectResponse(url="/admin/", status_code=303)


@app.post("/admin/agents/{agent_id}/delete")
def agent_delete(
    agent_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_login),
):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(404, "Agent not found")
    if user.role == UserRole.org_admin and agent.org_id != user.org_id:
        raise HTTPException(403, "Access denied")
    db.delete(agent)
    db.commit()
    return RedirectResponse(url="/admin/", status_code=303)


# ---------------------------------------------------------------------------
# Org management (super-admin only)
# ---------------------------------------------------------------------------

@app.get("/admin/orgs/new", response_class=HTMLResponse)
def org_new(request: Request, db: Session = Depends(get_db), user: User = Depends(require_super_admin)):
    return templates.TemplateResponse(request=request, name="org_form.html", context={
        "user": user, "org": None, "error": None,
    })


@app.post("/admin/orgs/new")
def org_create(
    request: Request,
    name: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_super_admin),
):
    existing = db.query(Org).filter(Org.name == name).first()
    if existing:
        return templates.TemplateResponse(request=request, name="org_form.html", context={
            "user": user, "org": None,
            "error": f"Org '{name}' already exists",
        }, status_code=400)
    org = Org(name=name)
    db.add(org)
    db.commit()
    return RedirectResponse(url=f"/admin/orgs/{org.id}", status_code=303)


@app.get("/admin/orgs/{org_id}", response_class=HTMLResponse)
def org_detail(
    org_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_super_admin),
):
    org = db.query(Org).filter(Org.id == org_id).first()
    if not org:
        raise HTTPException(404, "Org not found")
    users = db.query(User).filter(User.org_id == org_id).all()
    agents = db.query(Agent).filter(Agent.org_id == org_id).all()
    return templates.TemplateResponse(request=request, name="org_detail.html", context={
        "user": user, "org": org,
        "org_users": users, "agents": agents, "error": None,
    })


@app.post("/admin/orgs/{org_id}/toggle")
def org_toggle(
    org_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_super_admin),
):
    org = db.query(Org).filter(Org.id == org_id).first()
    if not org:
        raise HTTPException(404, "Org not found")
    org.is_active = not org.is_active
    db.commit()
    return RedirectResponse(url="/admin/", status_code=303)


# ---------------------------------------------------------------------------
# User management (super-admin, within an org)
# ---------------------------------------------------------------------------

@app.get("/admin/orgs/{org_id}/users/new", response_class=HTMLResponse)
def user_new(
    org_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_super_admin),
):
    org = db.query(Org).filter(Org.id == org_id).first()
    if not org:
        raise HTTPException(404, "Org not found")
    return templates.TemplateResponse(request=request, name="user_form.html", context={
        "user": user, "org": org, "error": None,
    })


@app.post("/admin/orgs/{org_id}/users/new")
def user_create(
    org_id: int,
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_super_admin),
):
    org = db.query(Org).filter(Org.id == org_id).first()
    if not org:
        raise HTTPException(404, "Org not found")
    existing = db.query(User).filter(User.username == username).first()
    if existing:
        return templates.TemplateResponse(request=request, name="user_form.html", context={
            "user": user, "org": org,
            "error": f"Username '{username}' already taken",
        }, status_code=400)
    new_user = User(
        org_id=org_id,
        username=username,
        password_hash=hash_password(password),
        role=UserRole.org_admin,
    )
    db.add(new_user)
    db.commit()
    return RedirectResponse(url=f"/admin/orgs/{org_id}", status_code=303)


@app.post("/admin/orgs/{org_id}/users/{user_id}/delete")
def user_delete(
    org_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_super_admin),
):
    target = db.query(User).filter(User.id == user_id, User.org_id == org_id).first()
    if not target:
        raise HTTPException(404, "User not found")
    db.delete(target)
    db.commit()
    return RedirectResponse(url=f"/admin/orgs/{org_id}", status_code=303)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}
