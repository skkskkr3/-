from __future__ import annotations

import hashlib
import hmac
import html as html_lib
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from difflib import SequenceMatcher
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
from docx import Document
from fastapi import Cookie, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from pypdf import PdfReader
import jwxt_adapter


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
FILES = DATA / "files"
PROFILES = DATA / "browser_profiles"
DB = DATA / "study.db"
BROWSER_CHANNEL = "msedge"
LOGIN_BROWSER_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--new-window",
    "--start-maximized",
]


def focus_login_window(context, keywords: tuple[str, ...] = ()) -> None:
    """Bring the Playwright-created Edge window to the foreground on Windows."""
    try:
        pages = [page for page in context.pages if not page.is_closed()]
        if pages:
            pages[0].bring_to_front()
        if os.name == "nt":
            import ctypes
            import ctypes.wintypes as wintypes
            user32 = ctypes.windll.user32
            found = []
            enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            def visit(hwnd, _lparam):
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                title = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, title, length + 1)
                value = title.value
                if value and (not keywords or any(word in value for word in keywords)):
                    found.append(hwnd)
                return True

            user32.EnumWindows(enum_proc(visit), 0)
            for hwnd in found:
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
                break
            user32.keybd_event(0x12, 0, 0, 0)
            user32.keybd_event(0x12, 0, 2, 0)
    except Exception:
        pass
LOGIN_TIMEOUT_SECONDS = 300
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".browsers"))
for folder in (DATA, FILES, PROFILES):
    folder.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="知序学生学习助手 MVP", version="0.3.0")
_sync_state: dict[int, dict] = {}
_login_state: dict[str, dict] = {}
_jwxt_login_state: dict[str, dict] = {}
_jwxt_sync_state: dict[int, dict] = {}
_state_lock = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_login_state(message: str, bind_user_id: int | None) -> dict:
    return {"state": "launching", "message": message, "bind_user_id": bind_user_id,
            "started_at": now(), "started_monotonic": time.monotonic(),
            "cancel_event": threading.Event()}


def reusable_login(store: dict[str, dict], bind_user_id: int | None, force: bool) -> str | None:
    """Reuse a live attempt, but never trap the UI behind a stale one."""
    for ticket, item in list(store.items()):
        if item.get("state") not in ("launching", "waiting") or item.get("bind_user_id") != bind_user_id:
            continue
        age = time.monotonic() - item.get("started_monotonic", 0)
        if not force and age < LOGIN_TIMEOUT_SECONDS:
            return ticket
        item["cancel_event"].set()
        store.pop(ticket, None)
    return None


def login_status(store: dict[str, dict], ticket: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", ticket):
        raise HTTPException(404, "登录请求不存在")
    with _state_lock:
        status = store.get(ticket)
        if status and status["state"] in ("launching", "waiting"):
            elapsed = int(time.monotonic() - status.get("started_monotonic", 0))
            if elapsed >= LOGIN_TIMEOUT_SECONDS:
                status["cancel_event"].set()
                status.update(state="error", message="登录等待已超时，请重新打开登录窗口")
        if status and status["state"] in ("success", "error"):
            store.pop(ticket, None)
    if not status:
        raise HTTPException(404, "登录请求已结束")
    return status


def cancel_login(store: dict[str, dict], ticket: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", ticket):
        raise HTTPException(404, "登录请求不存在")
    with _state_lock:
        status = store.pop(ticket, None)
        if status:
            status["cancel_event"].set()
    return {"ok": True}


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL, created_at TEXT NOT NULL,
          display_name TEXT NOT NULL DEFAULT '', profile_dir TEXT NOT NULL DEFAULT '',
          chaoxing_uid TEXT, jwxt_student_hash TEXT, jwxt_profile_dir TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
          expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS courses (
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
          external_id TEXT, name TEXT NOT NULL, teacher TEXT DEFAULT '',
          url TEXT DEFAULT '', created_at TEXT NOT NULL,
          UNIQUE(user_id, external_id)
        );
        CREATE TABLE IF NOT EXISTS assignments (
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
          course_id INTEGER NOT NULL REFERENCES courses(id),
          external_id TEXT, title TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'homework',
          description TEXT NOT NULL DEFAULT '', due_at TEXT,
          source_url TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'manual',
          status TEXT NOT NULL DEFAULT 'awaiting_start',
          draft TEXT NOT NULL DEFAULT '', draft_version INTEGER NOT NULL DEFAULT 0,
          approved_version INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(user_id, external_id)
        );
        CREATE TABLE IF NOT EXISTS attachments (
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
          assignment_id INTEGER NOT NULL REFERENCES assignments(id),
          original_name TEXT NOT NULL, stored_name TEXT NOT NULL,
          extracted_text TEXT NOT NULL DEFAULT '', sha256 TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
          assignment_id INTEGER REFERENCES assignments(id),
          kind TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS academic_cache (
          user_id INTEGER PRIMARY KEY REFERENCES users(id),
          schedule_json TEXT NOT NULL DEFAULT '[]', grades_json TEXT NOT NULL DEFAULT '[]',
          current_week INTEGER, updated_at TEXT NOT NULL
        );
        """)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        for name, definition in (
            ("display_name", "TEXT NOT NULL DEFAULT ''"),
            ("profile_dir", "TEXT NOT NULL DEFAULT ''"),
            ("chaoxing_uid", "TEXT"),
            ("jwxt_student_hash", "TEXT"),
            ("jwxt_profile_dir", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")
        assignment_columns = {row["name"] for row in conn.execute("PRAGMA table_info(assignments)")}
        for name, definition in (
            ("published_at", "TEXT"),
            ("platform_status", "TEXT NOT NULL DEFAULT 'unknown'"),
        ):
            if name not in assignment_columns:
                conn.execute(f"ALTER TABLE assignments ADD COLUMN {name} {definition}")
        academic_columns = {row["name"] for row in conn.execute("PRAGMA table_info(academic_cache)")}
        if "current_week" not in academic_columns:
            conn.execute("ALTER TABLE academic_cache ADD COLUMN current_week INTEGER")


init_db()


def require_user(session: str | None) -> int:
    if not session:
        raise HTTPException(401, "请先登录")
    digest = hashlib.sha256(session.encode()).hexdigest()
    with db() as conn:
        row = conn.execute("SELECT user_id FROM sessions WHERE token_hash=? AND expires_at>?", (digest, now())).fetchone()
    if not row:
        raise HTTPException(401, "登录已过期，请重新登录")
    return row["user_id"]


def make_session(response: Response, user_id: int) -> None:
    token = secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute("INSERT INTO sessions VALUES (?,?,?)", (
            hashlib.sha256(token.encode()).hexdigest(), user_id,
            (datetime.now(timezone.utc) + timedelta(days=14)).isoformat(timespec="seconds"),
        ))
    response.set_cookie("session", token, httponly=True, samesite="lax", max_age=14 * 86400)


class AssignmentIn(BaseModel):
    course_name: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=200)
    kind: str = "homework"
    description: str = Field(default="", max_length=30000)
    due_at: str | None = None
    source_url: str = ""


class DraftIn(BaseModel):
    draft: str = Field(max_length=100000)


@app.post("/api/logout")
def logout(response: Response, session: str | None = Cookie(default=None)):
    if session:
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(session.encode()).hexdigest(),))
    response.delete_cookie("session")
    return {"ok": True}


@app.get("/api/me")
def me(session: str | None = Cookie(default=None)):
    uid = require_user(session)
    with db() as conn:
        row = conn.execute("SELECT username,display_name FROM users WHERE id=?", (uid,)).fetchone()
    name = row["display_name"] or row["username"]
    return {"id": uid, "username": name, "name": name, "ai_available": bool(os.getenv("OPENAI_API_KEY"))}


def profile_path(uid: int) -> Path:
    with db() as conn:
        row = conn.execute("SELECT profile_dir FROM users WHERE id=?", (uid,)).fetchone()
    name = row["profile_dir"] if row else ""
    if not re.fullmatch(r"login_[0-9a-f]{32}", name):
        raise HTTPException(409, "请重新使用学习通登录")
    return PROFILES / name


def jwxt_profile_path(uid: int) -> Path:
    with db() as conn:
        row = conn.execute("SELECT jwxt_profile_dir FROM users WHERE id=?", (uid,)).fetchone()
    name = row["jwxt_profile_dir"] if row else ""
    if not re.fullmatch(r"jwxt_[0-9a-f]{32}", name):
        raise HTTPException(409, "请先使用教务系统登录")
    return PROFILES / name


def browser_profile_ready(profile: Path) -> bool:
    return any((profile / marker).exists() for marker in
               ("storage_state.json", "Preferences", "Default/Preferences"))


def open_saved_browser_context(playwright, profile: Path):
    state_file = profile / "storage_state.json"
    if state_file.exists():
        browser = playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
        return browser, browser.new_context(storage_state=str(state_file))
    return None, playwright.chromium.launch_persistent_context(
        str(profile), headless=True, channel=BROWSER_CHANNEL
    )


def close_saved_browser_context(browser, context, profile: Path) -> None:
    context.storage_state(path=str(profile / "storage_state.json"))
    context.close()
    if browser:
        browser.close()


def get_assignment(conn: sqlite3.Connection, uid: int, aid: int) -> sqlite3.Row:
    row = conn.execute("""SELECT a.*, c.name AS course_name FROM assignments a
                          JOIN courses c ON c.id=a.course_id
                          WHERE a.id=? AND a.user_id=?""", (aid, uid)).fetchone()
    if not row:
        raise HTTPException(404, "找不到这项任务")
    return row


def load_academic_cache(conn: sqlite3.Connection, uid: int) -> dict:
    row = conn.execute("SELECT schedule_json,grades_json,current_week,updated_at FROM academic_cache WHERE user_id=?", (uid,)).fetchone()
    if not row:
        return {"schedule": [], "grades": [], "current_week": None, "updated_at": None}
    try:
        return {"schedule": json.loads(row["schedule_json"]), "grades": json.loads(row["grades_json"]),
                "current_week": row["current_week"], "updated_at": row["updated_at"]}
    except (TypeError, json.JSONDecodeError):
        return {"schedule": [], "grades": [], "current_week": row["current_week"], "updated_at": row["updated_at"]}


def save_academic_cache(conn: sqlite3.Connection, uid: int, schedule: list, grades: list,
                        current_week: int | None = None) -> None:
    conn.execute("""INSERT INTO academic_cache(user_id,schedule_json,grades_json,current_week,updated_at) VALUES (?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET schedule_json=excluded.schedule_json,
        grades_json=excluded.grades_json,current_week=COALESCE(excluded.current_week,academic_cache.current_week),
        updated_at=excluded.updated_at""",
        (uid, json.dumps(schedule, ensure_ascii=False), json.dumps(grades, ensure_ascii=False), current_week, now()))


def normalize_course_name(value: str) -> str:
    """Reduce harmless platform naming differences without merging different course levels."""
    value = unicodedata.normalize("NFKC", value or "").lower()
    value = re.sub(r"\d{4}\s*[-—]\s*\d{2,4}", "", value)
    value = re.sub(r"(?:上|下)?学期", "", value)
    value = re.sub(r"[《》〈〉“”\"'\s_·\-—()（）]", "", value)
    value = re.sub(r"(?:上|下)$", "", value)
    value = re.sub(r"(?:课程|技术|基础)$", "", value)
    return value


def course_name_score(academic_name: str, learning_name: str) -> float:
    academic = normalize_course_name(academic_name)
    learning = normalize_course_name(learning_name)
    if not academic or not learning:
        return 0
    roman = r"(?:实训|实践)\s*([ivxⅠⅡⅢⅣⅤⅥ]+)"
    academic_level = re.search(roman, unicodedata.normalize("NFKC", academic_name), re.I)
    learning_level = re.search(roman, unicodedata.normalize("NFKC", learning_name), re.I)
    if academic_level and learning_level and academic_level.group(1).lower() != learning_level.group(1).lower():
        return 0
    if academic == learning:
        return 1
    if min(len(academic), len(learning)) >= 4 and (academic in learning or learning in academic):
        return .88
    return SequenceMatcher(None, academic, learning).ratio()


def valid_semester_schedule(schedule: list[dict]) -> list[dict]:
    """Drop Qiangzhi tooltip text accidentally exposed as a timetable cell."""
    result = []
    for item in schedule:
        name = str(item.get("course") or "").strip()
        if not name or len(name) > 70 or name.count(";") + name.count("；") > 1:
            continue
        result.append(item)
    return result


def belongs_to_current_semester(course_name: str, semester_names: list[str]) -> bool:
    return any(course_name_score(semester_name, course_name) >= .62 for semester_name in semester_names)


def current_semester_data(courses: list[dict], tasks: list[dict], schedule: list[dict]) -> tuple[list[dict], list[dict]]:
    """Merge the current JWXT timetable with matching Chaoxing courses, preserving raw history in SQLite."""
    task_counts: dict[int, int] = {}
    unfinished_counts: dict[int, int] = {}
    for task in tasks:
        course_id = task["course_id"]
        task_counts[course_id] = task_counts.get(course_id, 0) + 1
        if task.get("platform_status") != "completed":
            unfinished_counts[course_id] = unfinished_counts.get(course_id, 0) + 1
    if not schedule:
        fallback = [{
            **course, "key": f"course-{course['id']}", "learning_name": course["name"],
            "rooms": [], "schedule": [], "task_count": task_counts.get(course["id"], 0),
            "unfinished_count": unfinished_counts.get(course["id"], 0),
            "task_ids": [task["id"] for task in tasks if task["course_id"] == course["id"]],
            "has_learning": True,
        } for course in courses]
        return fallback, tasks
    names = list(dict.fromkeys(str(item.get("course") or "").strip() for item in schedule))
    used_ids: set[int] = set()
    semester_courses = []
    for index, academic_name in enumerate(name for name in names if name):
        candidates = []
        for course in courses:
            if course["id"] in used_ids:
                continue
            score = course_name_score(academic_name, course["name"])
            candidates.append((score, task_counts.get(course["id"], 0), course["id"], course))
        score, _, _, match = max(candidates, default=(0, 0, 0, None), key=lambda item: item[:3])
        if score < .62:
            match = None
        if match:
            used_ids.add(match["id"])
        rows = [item for item in schedule if item.get("course") == academic_name]
        course_id = match["id"] if match else None
        semester_courses.append({
            **(match or {}), "id": course_id, "key": f"course-{course_id}" if course_id else f"jwxt-{index}",
            "name": academic_name, "learning_name": match["name"] if match else "",
            "teacher": "、".join(dict.fromkeys(row.get("teacher", "") for row in rows if row.get("teacher"))),
            "rooms": list(dict.fromkeys(row.get("room", "") for row in rows if row.get("room"))),
            "schedule": rows, "task_count": task_counts.get(course_id, 0) if course_id else 0,
            "unfinished_count": unfinished_counts.get(course_id, 0) if course_id else 0,
            "task_ids": [task["id"] for task in tasks if task["course_id"] == course_id] if course_id else [],
            "has_learning": bool(match),
        })
    current_ids = used_ids
    current_tasks = [task for task in tasks if task["course_id"] in current_ids or task.get("source") == "manual"]
    return semester_courses, current_tasks


@app.get("/api/dashboard")
def dashboard(session: str | None = Cookie(default=None)):
    uid = require_user(session)
    create_deadline_reminders(uid)
    with db() as conn:
        all_courses = [dict(r) for r in conn.execute("SELECT * FROM courses WHERE user_id=? ORDER BY name", (uid,))]
        all_tasks = [dict(r) for r in conn.execute("""SELECT a.*, c.name AS course_name FROM assignments a
            JOIN courses c ON c.id=a.course_id WHERE a.user_id=? ORDER BY
            CASE a.platform_status WHEN 'unfinished' THEN 0 WHEN 'unknown' THEN 1 ELSE 2 END,
            COALESCE(a.published_at,a.created_at) DESC, a.id DESC""", (uid,))]
        events = [dict(r) for r in conn.execute("SELECT * FROM events WHERE user_id=? ORDER BY id DESC LIMIT 12", (uid,))]
        jwxt = load_academic_cache(conn, uid)
    jwxt["schedule"] = valid_semester_schedule(jwxt["schedule"])
    jwxt["grade_summary"] = jwxt_adapter.grade_summary(jwxt["grades"])
    courses, tasks = current_semester_data(all_courses, all_tasks, jwxt["schedule"])
    try:
        connected = browser_profile_ready(profile_path(uid))
    except HTTPException:
        connected = False
    try:
        jwxt_connected = browser_profile_ready(jwxt_profile_path(uid))
    except HTTPException:
        jwxt_connected = False
    if connected:
        queue_sync(uid, _sync_state, sync_browser, "正在自动读取学习通课程和作业")
    if jwxt_connected:
        queue_sync(uid, _jwxt_sync_state, sync_jwxt_browser, "正在自动读取课表和成绩")
    with _state_lock:
        sync = dict(_sync_state.get(uid, {"state": "idle", "message": "尚未同步学习通课程"}))
        jwxt["sync"] = dict(_jwxt_sync_state.get(uid, {"state": "idle", "message": "尚未读取教务数据"}))
        logins = {
            "chaoxing": next((ticket for ticket, item in _login_state.items()
                               if item.get("bind_user_id") == uid and item.get("state") in ("launching", "waiting")), None),
            "jwxt": next((ticket for ticket, item in _jwxt_login_state.items()
                           if item.get("bind_user_id") == uid and item.get("state") in ("launching", "waiting")), None),
        }
    jwxt["connected"] = jwxt_connected
    return {"courses": courses, "tasks": tasks, "events": events, "sync": sync,
            "semester_scope": "current" if jwxt["schedule"] else "learning_fallback",
            "archived_course_count": max(0, len(all_courses) - sum(1 for course in courses if course.get("id"))),
            "connected": connected, "jwxt": jwxt, "logins": logins}


def create_deadline_reminders(uid: int) -> None:
    """Create in-app reminders when a student opens the dashboard."""
    current = datetime.now(timezone.utc)
    with db() as conn:
        rows = conn.execute("SELECT id,title,due_at,status,platform_status FROM assignments WHERE user_id=? AND due_at IS NOT NULL", (uid,)).fetchall()
        for task in rows:
            if task["status"] == "approved" or task["platform_status"] == "completed":
                continue
            try:
                due = datetime.fromisoformat(task["due_at"].replace("Z", "+00:00"))
                if due.tzinfo is None:
                    due = due.replace(tzinfo=timezone.utc)
                hours = (due - current).total_seconds() / 3600
            except ValueError:
                continue
            if hours < 0:
                kind, label = "overdue", "已过截止时间"
            elif hours <= 24:
                kind, label = "due_24h", "24 小时内截止"
            elif hours <= 72:
                kind, label = "due_3d", "3 天内截止"
            elif hours <= 168:
                kind, label = "due_7d", "7 天内截止"
            else:
                continue
            exists = conn.execute("SELECT 1 FROM events WHERE user_id=? AND assignment_id=? AND kind=? LIMIT 1",
                                  (uid, task["id"], kind)).fetchone()
            if not exists:
                add_event(conn, uid, kind, f"{task['title']}：{label}", task["id"])


def add_event(conn: sqlite3.Connection, uid: int, kind: str, message: str, aid: int | None = None):
    conn.execute("INSERT INTO events(user_id,assignment_id,kind,message,created_at) VALUES (?,?,?,?,?)",
                 (uid, aid, kind, message, now()))


def ensure_course(conn: sqlite3.Connection, uid: int, name: str, external_id: str | None = None,
                  url: str = "") -> int:
    if external_id:
        row = conn.execute("SELECT id FROM courses WHERE user_id=? AND external_id=?", (uid, external_id)).fetchone()
    else:
        row = conn.execute("SELECT id FROM courses WHERE user_id=? AND name=?", (uid, name)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO courses(user_id,external_id,name,url,created_at) VALUES (?,?,?,?,?)",
                       (uid, external_id, name, url, now()))
    return cur.lastrowid


@app.post("/api/tasks")
def create_task(payload: AssignmentIn, session: str | None = Cookie(default=None)):
    uid = require_user(session)
    if payload.kind not in ("homework", "lab", "report", "exam"):
        raise HTTPException(400, "不支持的任务类型")
    with db() as conn:
        cid = ensure_course(conn, uid, payload.course_name.strip())
        cur = conn.execute("""INSERT INTO assignments
            (user_id,course_id,title,kind,description,due_at,source_url,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (uid, cid, payload.title.strip(), payload.kind, payload.description.strip(),
             payload.due_at or None, payload.source_url.strip(), now(), now()))
        add_event(conn, uid, "created", f"已添加任务：{payload.title}", cur.lastrowid)
    return {"id": cur.lastrowid}


@app.put("/api/tasks/{aid}")
def update_task(aid: int, payload: AssignmentIn, session: str | None = Cookie(default=None)):
    uid = require_user(session)
    if payload.kind not in ("homework", "lab", "report", "exam"):
        raise HTTPException(400, "不支持的任务类型")
    with db() as conn:
        old = get_assignment(conn, uid, aid)
        cid = ensure_course(conn, uid, payload.course_name.strip())
        conn.execute("""UPDATE assignments SET course_id=?,title=?,kind=?,description=?,due_at=?,
          source_url=?,approved_version=NULL,status=?,updated_at=? WHERE id=? AND user_id=?""",
          (cid, payload.title.strip(), payload.kind, payload.description.strip(), payload.due_at or None,
           payload.source_url.strip(), "awaiting_review" if old["draft"] else "awaiting_start", now(), aid, uid))
        add_event(conn, uid, "updated", "用户修正了任务信息；旧批准已失效", aid)
    return {"ok": True}


@app.get("/api/tasks/{aid}")
def task_detail(aid: int, session: str | None = Cookie(default=None)):
    uid = require_user(session)
    with db() as conn:
        task = dict(get_assignment(conn, uid, aid))
        attachments = [dict(r) for r in conn.execute("SELECT id,original_name,sha256,created_at FROM attachments WHERE user_id=? AND assignment_id=?", (uid, aid))]
    return {"task": task, "attachments": attachments}


@app.delete("/api/tasks/{aid}")
def delete_task(aid: int, session: str | None = Cookie(default=None)):
    uid = require_user(session)
    with db() as conn:
        task = get_assignment(conn, uid, aid)
        if task["source"] != "manual":
            raise HTTPException(409, "学习通同步任务不能删除；下次同步时仍会重新出现")
        conn.execute("DELETE FROM events WHERE user_id=? AND assignment_id=?", (uid, aid))
        conn.execute("DELETE FROM attachments WHERE user_id=? AND assignment_id=?", (uid, aid))
        conn.execute("DELETE FROM assignments WHERE id=? AND user_id=?", (aid, uid))
    shutil.rmtree(FILES / str(uid) / str(aid), ignore_errors=True)
    return {"ok": True}


def extract_text(name: str, path: Path) -> str:
    ext = Path(name).suffix.lower()
    try:
        if ext in (".txt", ".md", ".csv", ".py", ".json"):
            return path.read_text(encoding="utf-8", errors="replace")[:40000]
        if ext == ".pdf":
            return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)[:40000]
        if ext == ".docx":
            return "\n".join(p.text for p in Document(str(path)).paragraphs)[:40000]
    except Exception:
        return ""
    return ""


def chaoxing_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "chaoxing.com" or host.endswith(".chaoxing.com"))


def fetch_source(uid: int, aid: int) -> dict:
    with db() as conn:
        task = dict(get_assignment(conn, uid, aid))
    if not chaoxing_url(task["source_url"]):
        raise HTTPException(400, "当前任务没有可读取的学习通 HTTPS 原始链接")
    browser_profile = profile_path(uid)
    if not browser_profile_ready(browser_profile):
        raise HTTPException(409, "请先使用学习通登录")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser, context = open_saved_browser_context(p, browser_profile)
        ensure_profile_matches(context, uid)
        page = context.new_page()
        page.goto(task["source_url"], wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(800)
        if "passport" in page.url.lower() or "login" in page.url.lower():
            close_saved_browser_context(browser, context, browser_profile)
            raise HTTPException(409, "学习通登录会话已失效，请重新连接")
        soup = BeautifulSoup(page.content(), "html.parser")
        for node in soup.select("script,style,nav,footer,header"):
            node.decompose()
        source_text = soup.get_text("\n", strip=True)[:30000]
        links = []
        for anchor in soup.select("a[href]"):
            href = urljoin(page.url, anchor.get("href", ""))
            label = anchor.get_text(" ", strip=True) or Path(urlparse(href).path).name
            suffix = Path(urlparse(href).path).suffix.lower()
            if chaoxing_url(href) and suffix in (".pdf", ".docx", ".txt", ".md", ".csv", ".py", ".json"):
                links.append((href, label[:150] or "课程资料"))
        downloaded = 0
        for href, label in links[:12]:
            try:
                response = context.request.get(href, timeout=20000, headers={"Referer": page.url})
                if not response.ok:
                    continue
                body = response.body()
                if not body or len(body) > 10 * 1024 * 1024:
                    continue
                digest = hashlib.sha256(body).hexdigest()
                with db() as conn:
                    exists = conn.execute("SELECT id FROM attachments WHERE user_id=? AND assignment_id=? AND sha256=?", (uid, aid, digest)).fetchone()
                if exists:
                    continue
                folder = FILES / str(uid) / str(aid)
                folder.mkdir(parents=True, exist_ok=True)
                name = Path(urlparse(href).path).name or label
                stored = secrets.token_hex(16) + Path(name).suffix.lower()
                path = folder / stored
                path.write_bytes(body)
                with db() as conn:
                    conn.execute("""INSERT INTO attachments
                      (user_id,assignment_id,original_name,stored_name,extracted_text,sha256,created_at)
                      VALUES (?,?,?,?,?,?,?)""", (uid, aid, name, stored, extract_text(name, path), digest, now()))
                downloaded += 1
            except Exception:
                continue
        close_saved_browser_context(browser, context, browser_profile)
    if len(source_text) < 20:
        raise HTTPException(422, "原始页面内容过少，请检查页面或手动上传资料")
    with db() as conn:
        conn.execute("UPDATE assignments SET description=?,updated_at=? WHERE id=? AND user_id=?",
                     (source_text, now(), aid, uid))
        add_event(conn, uid, "source", f"已读取原始页面，下载 {downloaded} 份可识别的资料", aid)
    return {"text_length": len(source_text), "downloaded": downloaded}


@app.post("/api/tasks/{aid}/fetch-source")
def fetch_source_route(aid: int, session: str | None = Cookie(default=None)):
    uid = require_user(session)
    return fetch_source(uid, aid)


@app.post("/api/tasks/{aid}/attachments")
async def upload_attachment(aid: int, file: UploadFile = File(...), session: str | None = Cookie(default=None)):
    uid = require_user(session)
    with db() as conn:
        get_assignment(conn, uid, aid)
    name = Path(file.filename or "attachment").name
    if Path(name).suffix.lower() not in (".txt", ".md", ".csv", ".py", ".json", ".pdf", ".docx", ".png", ".jpg", ".jpeg"):
        raise HTTPException(400, "暂不支持这种附件格式")
    content = await file.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(413, "单个附件不得超过 10 MB")
    folder = FILES / str(uid) / str(aid)
    folder.mkdir(parents=True, exist_ok=True)
    stored = secrets.token_hex(16) + Path(name).suffix.lower()
    path = folder / stored
    path.write_bytes(content)
    extracted = extract_text(name, path)
    with db() as conn:
        cur = conn.execute("""INSERT INTO attachments
          (user_id,assignment_id,original_name,stored_name,extracted_text,sha256,created_at)
          VALUES (?,?,?,?,?,?,?)""", (uid, aid, name, stored, extracted,
             hashlib.sha256(content).hexdigest(), now()))
        add_event(conn, uid, "attachment", f"已添加资料：{name}", aid)
    return {"id": cur.lastrowid, "extracted": bool(extracted)}


@app.get("/api/tasks/{aid}/attachments/{file_id}")
def download_attachment(aid: int, file_id: int, session: str | None = Cookie(default=None)):
    uid = require_user(session)
    with db() as conn:
        row = conn.execute("SELECT * FROM attachments WHERE id=? AND user_id=? AND assignment_id=?", (file_id, uid, aid)).fetchone()
    if not row:
        raise HTTPException(404, "附件不存在")
    return FileResponse(FILES / str(uid) / str(aid) / row["stored_name"], filename=row["original_name"])


def build_fallback_plan(task: dict, attachments: list[dict]) -> str:
    lines = [f"# {task['title']}｜任务执行计划", "", "此为自动整理的执行计划，尚未生成可交付作业。", "",
             "## 已知要求", task["description"] or "任务说明尚未填写，请先补充原始要求。", "",
             "## 已收集资料"]
    lines += [f"- {a['original_name']}" for a in attachments] or ["- 暂无资料，请上传老师下发的文件。"]
    lines += ["", "## 建议步骤", "1. 核对任务要求与提交格式。", "2. 阅读课程资料并确认评分点。",
              "3. 完成实验或草稿，记录真实过程与结果。", "4. 对照要求检查并由本人审核。"]
    return "\n".join(lines)


def call_ai(task: dict, attachments: list[dict]) -> str:
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        return build_fallback_plan(task, attachments)
    source = "\n\n".join(f"[{a['original_name']}]\n{a['extracted_text'][:12000]}" for a in attachments if a["extracted_text"])
    instructions = (
        "你是学生的课程任务助手。依据用户提供的任务说明和资料，生成可审核的 Markdown 工作草稿。"
        "先列出要求与资料来源，再给出完成步骤、已有依据的草稿内容、待学生实际完成或核实的事项。"
        "不得编造实验数据、截图、引用、运行结果、教师要求或个人经历；缺少证据的地方明确标记【待核实】。"
        "考试和限时测验仅给学习计划，不生成答案。"
    )
    body = json.dumps({"model": os.getenv("OPENAI_MODEL", "gpt-6-luna"),
                       "instructions": instructions,
                       "input": f"任务类型：{task['kind']}\n标题：{task['title']}\n说明：{task['description']}\n资料：\n{source[:45000]}",
                       "store": False}, ensure_ascii=False).encode()
    req = urllib.request.Request("https://api.openai.com/v1/responses", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as res:
        result = json.load(res)
    chunks = [c.get("text", "") for item in result.get("output", [])
              if item.get("type") == "message" for c in item.get("content", []) if c.get("type") == "output_text"]
    return "\n".join(chunks).strip() or build_fallback_plan(task, attachments)


@app.post("/api/tasks/{aid}/start")
def start_task(aid: int, session: str | None = Cookie(default=None)):
    uid = require_user(session)
    with db() as conn:
        task = dict(get_assignment(conn, uid, aid))
        attachments = [dict(r) for r in conn.execute("SELECT * FROM attachments WHERE user_id=? AND assignment_id=?", (uid, aid))]
    if task["kind"] == "exam":
        raise HTTPException(400, "考试任务仅支持提醒与资料管理")
    if task["status"] == "running":
        raise HTTPException(409, "任务正在执行")
    with db() as conn:
        conn.execute("UPDATE assignments SET status='running',updated_at=? WHERE id=? AND user_id=?", (now(), aid, uid))
        add_event(conn, uid, "started", "用户启动任务执行", aid)
    try:
        draft = call_ai(task, attachments)
        final_status = "awaiting_review" if os.getenv("OPENAI_API_KEY") else "planned"
        with db() as conn:
            conn.execute("""UPDATE assignments SET draft=?,draft_version=draft_version+1,
              approved_version=NULL,status=?,updated_at=? WHERE id=? AND user_id=?""",
              (draft, final_status, now(), aid, uid))
            add_event(conn, uid, "draft", "已生成 AI 草稿，等待审核" if final_status == "awaiting_review" else "已生成任务执行计划；配置 AI 后可生成草稿", aid)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        with db() as conn:
            conn.execute("UPDATE assignments SET status='needs_attention',updated_at=? WHERE id=? AND user_id=?", (now(), aid, uid))
            add_event(conn, uid, "error", "AI 生成失败，请检查 API 配置后重试", aid)
        raise HTTPException(502, f"AI 生成失败：{str(exc)[:200]}")
    return {"status": final_status}


@app.put("/api/tasks/{aid}/draft")
def save_draft(aid: int, payload: DraftIn, session: str | None = Cookie(default=None)):
    uid = require_user(session)
    with db() as conn:
        get_assignment(conn, uid, aid)
        conn.execute("""UPDATE assignments SET draft=?,draft_version=draft_version+1,
          approved_version=NULL,status='awaiting_review',updated_at=? WHERE id=? AND user_id=?""",
          (payload.draft, now(), aid, uid))
        add_event(conn, uid, "edited", "用户修改了草稿，旧批准已失效", aid)
    return {"ok": True}


@app.post("/api/tasks/{aid}/approve")
def approve(aid: int, session: str | None = Cookie(default=None)):
    uid = require_user(session)
    with db() as conn:
        task = get_assignment(conn, uid, aid)
        if task["status"] != "awaiting_review" or not task["draft"].strip():
            raise HTTPException(409, "当前没有可批准的草稿")
        conn.execute("UPDATE assignments SET approved_version=draft_version,status='approved',updated_at=? WHERE id=? AND user_id=?",
                     (now(), aid, uid))
        add_event(conn, uid, "approved", f"用户批准草稿版本 v{task['draft_version']}；尚未提交学习通", aid)
    return {"ok": True}


@app.get("/api/tasks/{aid}/export")
def export_draft(aid: int, session: str | None = Cookie(default=None)):
    uid = require_user(session)
    with db() as conn:
        task = get_assignment(conn, uid, aid)
    if not task["draft"]:
        raise HTTPException(404, "尚无草稿")
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(task["draft"], headers={"Content-Disposition": f'attachment; filename="assignment-{aid}.md"'})


def set_state(uid: int, state: str, message: str):
    with _state_lock:
        _sync_state[uid] = {"state": state, "message": message, "updated_at": now()}


def queue_sync(uid: int, states: dict, worker, message: str, force: bool = False) -> bool:
    with _state_lock:
        previous = states.get(uid, {})
        if previous.get("state") in ("queued", "syncing"):
            return False
        if previous.get("state") == "needs_reauth" and not force:
            return False
        last = previous.get("updated_at")
        if not force and last and datetime.now(timezone.utc) - datetime.fromisoformat(last) < timedelta(minutes=30):
            return False
        states[uid] = {"state": "queued", "message": message, "updated_at": now()}
    threading.Thread(target=worker, args=(uid,), daemon=True).start()
    return True


def parse_all_task(html: str) -> list[dict]:
    """Best-effort parser. Site variants must be checked against a real student account."""
    soup = BeautifulSoup(html, "html.parser")
    result = []

    def platform_status(text: str) -> str:
        if any(marker in text for marker in ("未提交", "待完成", "未完成", "待互评")):
            return "unfinished"
        if any(marker in text for marker in ("已完成", "已提交", "已批阅", "待批阅", "已互评")):
            return "completed"
        return "unknown"

    def deadline(text: str) -> str | None:
        match = re.search(r"(?:截止|结束)(?:时间)?[：:\s]*((?:20\d{2}[-/])?\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2})", text)
        if not match:
            match = re.search(r"((?:20\d{2}[-/])?\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2})", text)
        if not match:
            return None
        raw = match.group(1).replace("/", "-")
        if not raw.startswith("20"):
            raw = f"{datetime.now().year}-{raw}"
        try:
            return datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc).isoformat()
        except ValueError:
            return None

    for item in soup.select(".task-list li[data]"):
        title_node = item.select_one(".overHidden2")
        course_node = item.select_one(".courseName")
        href = html_lib.unescape(item.get("data", "")).strip()
        title = title_node.get_text(" ", strip=True) if title_node else ""
        context = item.get_text(" ", strip=True)
        if title and href and len(title) <= 180 and chaoxing_url(href):
            result.append({"title": title, "course": course_node.get_text(" ", strip=True) if course_node else "学习通课程",
                           "due_at": deadline(context), "published_at": None,
                           "platform_status": platform_status(context), "href": href, "context": context})

    for a in soup.select("a[href]"):
        title = a.get_text(" ", strip=True)
        href = a.get("href", "")
        parent = a.find_parent(["li", "tr", "div"])
        context = parent.get_text(" ", strip=True) if parent else title
        if not title or len(title) > 180 or len(context) > 1000:
            continue
        if not ("work" in href.lower() or "作业" in context or "实验" in context):
            continue
        if title in ("作业", "我的作业", "全部作业", "查看", "详情"):
            continue
        if href.startswith("javascript:") or href == "#":
            continue
        course = (parent.get("data-course-name", "").strip() if parent else "") or "学习通课程"
        result.append({"title": title, "course": course, "due_at": deadline(context), "published_at": None,
                       "platform_status": platform_status(context), "href": href, "context": context})
    unique = {x["href"]: x for x in result}
    return list(unique.values())


def parse_chaoxing_answer_window(html: str) -> tuple[str | None, str | None]:
    """Return the assignment open/publish time and deadline when Chaoxing exposes them."""
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    match = re.search(r"作答时间\s*[：:]?\s*((?:20\d{2}[-/])?\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2})"
                      r"(?:\s*(?:至|~|—|-)\s*((?:20\d{2}[-/])?\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}))?", text)
    if not match:
        return None, None

    def iso(value: str | None) -> str | None:
        if not value:
            return None
        value = value.replace("/", "-")
        if not value.startswith("20"):
            value = f"{datetime.now().year}-{value}"
        try:
            local = datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=timezone(timedelta(hours=8)))
            return local.astimezone(timezone.utc).isoformat()
        except ValueError:
            return None

    first, second = iso(match.group(1)), iso(match.group(2))
    return (first, second) if second else (None, first)


def chaoxing_assignment_key(href: str) -> str:
    query = {key.lower(): values for key, values in parse_qs(urlparse(href).query).items()}
    parts = [(query.get(key) or [""])[0] for key in ("courseid", "classid", "workid")]
    return "chaoxing:" + ":".join(parts) if all(part.isdigit() for part in parts) else href


def normalize_chaoxing_assignments(conn: sqlite3.Connection, uid: int) -> None:
    groups: dict[str, list[sqlite3.Row]] = {}
    rows = conn.execute("""SELECT id,external_id,due_at,published_at,platform_status FROM assignments
        WHERE user_id=? AND source='chaoxing'""", (uid,)).fetchall()
    for row in rows:
        groups.setdefault(chaoxing_assignment_key(row["external_id"]), []).append(row)
    for key, duplicates in groups.items():
        keeper = min(duplicates, key=lambda row: row["id"])
        status = next((row["platform_status"] for row in reversed(duplicates)
                       if row["platform_status"] != "unknown"), keeper["platform_status"])
        due_at = next((row["due_at"] for row in reversed(duplicates) if row["due_at"]), None)
        published_at = next((row["published_at"] for row in reversed(duplicates) if row["published_at"]), None)
        for duplicate in duplicates:
            if duplicate["id"] == keeper["id"]:
                continue
            conn.execute("UPDATE attachments SET assignment_id=? WHERE assignment_id=?", (keeper["id"], duplicate["id"]))
            conn.execute("UPDATE events SET assignment_id=? WHERE assignment_id=?", (keeper["id"], duplicate["id"]))
            conn.execute("DELETE FROM assignments WHERE id=?", (duplicate["id"],))
        conn.execute("""UPDATE assignments SET external_id=?,due_at=COALESCE(?,due_at),
            published_at=COALESCE(?,published_at),platform_status=? WHERE id=?""",
            (key, due_at, published_at, status, keeper["id"]))


def parse_chaoxing_profile(html: str) -> dict | None:
    uid_match = re.search(r"\bvar\s+uid\s*=\s*['\"]?(\d+)", html)
    if not uid_match:
        return None
    name_match = re.search(r'aria-label=["\']账号：([^"\'<]{1,40})', html)
    school_match = re.search(r'id=["\']siteName["\'][^>]*title=["\']([^"\']+)', html)
    fid_match = re.search(r'\bvar\s+fid\s*=\s*["\']?(\d+)', html) or re.search(r'["\']fid["\']\s*:\s*["\']?(\d+)', html)
    return {"uid": uid_match.group(1),
            "name": name_match.group(1).strip() if name_match else "学习通用户",
            "school": school_match.group(1).strip() if school_match else "",
            "fid": fid_match.group(1) if fid_match else ""}


def parse_chaoxing_session(cookies: list[dict], html: str = "") -> dict | None:
    values = {str(cookie.get("name", "")).lower(): str(cookie.get("value", ""))
              for cookie in cookies if str(cookie.get("domain", "")).lower().endswith("chaoxing.com")}
    uid = values.get("uid") or values.get("_uid") or ""
    if not uid.isdigit():
        return None
    visible = parse_chaoxing_profile(html) or {}
    fid = values.get("fid", "")
    return {"uid": uid, "name": visible.get("name", "学习通用户"),
            "school": visible.get("school", ""), "fid": fid if fid.isdigit() else visible.get("fid", "")}


def identity_from_context(context, current_page_html: str = "") -> dict | None:
    profile = parse_chaoxing_profile(current_page_html)
    if not profile:
        try:
            profile = parse_chaoxing_session(context.cookies(), current_page_html)
        except Exception:
            pass
    if profile:
        return profile
    for url in ("https://i.chaoxing.com/base", "https://i.mooc.chaoxing.com/space/index"):
        try:
            response = context.request.get(url, timeout=20000)
            if response.ok and urlparse(response.url).hostname in ("i.chaoxing.com", "i.mooc.chaoxing.com"):
                profile = parse_chaoxing_profile(response.text())
                if profile:
                    return profile
        except Exception:
            continue
    return None


def ensure_profile_matches(context, uid: int) -> dict:
    profile = identity_from_context(context)
    with db() as conn:
        row = conn.execute("SELECT chaoxing_uid FROM users WHERE id=?", (uid,)).fetchone()
    if not profile or not row or profile["uid"] != row["chaoxing_uid"]:
        raise RuntimeError("学习通登录已过期或账号已切换，请退出知序后重新登录")
    return profile


def parse_chaoxing_courses(html: str) -> list[dict]:
    found = []
    seen = set()
    for match in re.finditer(r"stucoursemiddle\?([^\"'\s<>]+)", html, flags=re.IGNORECASE):
        query = parse_qs(html_lib.unescape(match.group(1)))
        course_id = (query.get("courseid") or query.get("courseId") or [""])[0]
        clazz_id = (query.get("clazzid") or query.get("classId") or [""])[0]
        cpi = (query.get("cpi") or [""])[0]
        key = (course_id, clazz_id, cpi)
        if not all(part.isdigit() for part in key) or key in seen:
            continue
        seen.add(key)
        nearby = html[match.end():match.end() + 1800]
        title = re.search(r'course-name[^>]*title=["\']([^"\']+)', nearby)
        if not title:
            continue
        name = html_lib.unescape(title.group(1)).strip()
        if not name:
            continue
        url = f"https://mooc1.chaoxing.com/visit/stucoursemiddle?courseid={course_id}&clazzid={clazz_id}&cpi={cpi}"
        found.append({"name": name[:120], "href": url, "external_id": f"{course_id}:{clazz_id}:{cpi}"})
    return found


def login_browser(ticket: str):
    folder_name = "login_" + ticket
    with _state_lock:
        item = _login_state[ticket]
        bind_user_id = item.get("bind_user_id")
        cancel_event = item["cancel_event"]
        _login_state[ticket]["message"] = "正在启动 Edge 学习通登录窗口"
    context = None
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(PROFILES / folder_name), headless=False, channel=BROWSER_CHANNEL,
                args=LOGIN_BROWSER_ARGS, timeout=20000)
            page = context.pages[0] if context.pages else context.new_page()
            focus_login_window(context, ("Microsoft Edge",))
            page.goto("https://i.mooc.chaoxing.com/space/index", wait_until="commit", timeout=20000)
            focus_login_window(context, ("学习通", "超星", "Microsoft Edge"))
            with _state_lock:
                cancelled = ticket not in _login_state or cancel_event.is_set()
                if not cancelled:
                    _login_state[ticket].update(state="waiting", message="请在弹出的学习通页面完成登录")
            if cancelled:
                context.close()
                context = None
                return
            identity = None
            for _ in range(120):
                if cancel_event.is_set():
                    context.close()
                    context = None
                    raise RuntimeError("登录已取消")
                if all(tab.is_closed() for tab in context.pages):
                    raise RuntimeError("登录窗口已关闭")
                for tab in context.pages:
                    if tab.is_closed():
                        continue
                    host = (urlparse(tab.url).hostname or "").lower()
                    if host == "chaoxing.com" or host.endswith(".chaoxing.com"):
                        identity = identity_from_context(context, tab.content())
                        if identity:
                            break
                if identity:
                    break
                time.sleep(2)
            context.storage_state(path=str(PROFILES / folder_name / "storage_state.json"))
            context.close()
            context = None
        if not identity:
            raise RuntimeError("尚未检测到完整的学习通登录信息；请确认页面已进入“我学的课”")
        if cancel_event.is_set():
            return
        with db() as conn:
            username = "xxt_" + identity["uid"]
            row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
            owner = conn.execute("SELECT id FROM users WHERE chaoxing_uid=?", (identity["uid"],)).fetchone()
            if bind_user_id:
                if owner and owner["id"] != bind_user_id:
                    raise RuntimeError("这个学习通账号已关联另一学习空间；请先使用原账号登录")
                current = conn.execute("SELECT chaoxing_uid FROM users WHERE id=?", (bind_user_id,)).fetchone()
                if not current or (current["chaoxing_uid"] and current["chaoxing_uid"] != identity["uid"]):
                    raise RuntimeError("当前学习空间已关联其他学习通账号")
                user_id = bind_user_id
                conn.execute("UPDATE users SET profile_dir=?,chaoxing_uid=? WHERE id=?",
                             (folder_name, identity["uid"], user_id))
            elif owner:
                user_id = owner["id"]
                conn.execute("UPDATE users SET display_name=?,profile_dir=? WHERE id=?",
                             (identity["name"], folder_name, user_id))
            elif row:
                user_id = row["id"]
                conn.execute("UPDATE users SET display_name=?,profile_dir=?,chaoxing_uid=? WHERE id=?",
                             (identity["name"], folder_name, identity["uid"], user_id))
            else:
                cur = conn.execute("""INSERT INTO users(username,password_hash,created_at,display_name,profile_dir,chaoxing_uid)
                  VALUES (?,?,?,?,?,?)""", (username, "!external-login", now(), identity["name"], folder_name, identity["uid"]))
                user_id = cur.lastrowid
        with _state_lock:
            if cancel_event.is_set() or ticket not in _login_state:
                return
            _sync_state.pop(user_id, None)
            _login_state[ticket].update(state="success", message="登录成功", user_id=user_id,
                                        display_name=identity["name"])
    except Exception as exc:
        with _state_lock:
            if ticket in _login_state:
                _login_state[ticket].update(state="error", message=f"学习通登录未完成：{str(exc)[:160]}")
    finally:
        if context:
            try:
                context.close()
            except Exception:
                pass


@app.post("/api/chaoxing/login/start")
def start_chaoxing_login(request: Request, force: bool = False, session: str | None = Cookie(default=None)):
    origin = request.headers.get("origin")
    if origin and origin not in ("http://127.0.0.1:8000", "http://localhost:8000"):
        raise HTTPException(403, "登录请求来源不匹配")
    bind_user_id = require_user(session) if session else None
    with _state_lock:
        other_ticket = reusable_login(_jwxt_login_state, bind_user_id, False)
        if other_ticket and not force:
            raise HTTPException(409, "已有教务系统登录窗口，请先完成、取消或重新打开")
        if other_ticket:
            reusable_login(_jwxt_login_state, bind_user_id, True)
        active_ticket = reusable_login(_login_state, bind_user_id, force)
        if active_ticket:
            return {"ticket": active_ticket, "resumed": True}
        ticket = secrets.token_hex(16)
        _login_state[ticket] = new_login_state("正在打开学习通", bind_user_id)
    threading.Thread(target=login_browser, args=(ticket,), daemon=True).start()
    return {"ticket": ticket}


@app.get("/api/chaoxing/login/status/{ticket}")
def chaoxing_login_status(ticket: str, response: Response):
    status = login_status(_login_state, ticket)
    if status["state"] == "success":
        make_session(response, status["user_id"])
    elapsed = int(time.monotonic() - status.get("started_monotonic", time.monotonic()))
    return {"state": status["state"], "message": status["message"], "elapsed": max(0, elapsed)}


@app.post("/api/chaoxing/login/cancel/{ticket}")
def cancel_chaoxing_login(ticket: str):
    return cancel_login(_login_state, ticket)


def jwxt_student_hash(number: str) -> str:
    key_file = DATA / "identity.key"
    if not key_file.exists():
        try:
            with key_file.open("xb") as handle:
                handle.write(secrets.token_bytes(32))
        except FileExistsError:
            pass
    return hmac.new(key_file.read_bytes(), number.encode(), hashlib.sha256).hexdigest()


def jwxt_login_browser(ticket: str):
    folder_name = "jwxt_" + ticket
    with _state_lock:
        item = _jwxt_login_state[ticket]
        bind_user_id = item.get("bind_user_id")
        cancel_event = item["cancel_event"]
        _jwxt_login_state[ticket]["message"] = "正在启动 Edge 教务系统登录窗口"
    context = None
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(PROFILES / folder_name), headless=False, channel=BROWSER_CHANNEL,
                args=LOGIN_BROWSER_ARGS, timeout=20000)
            page = context.pages[0] if context.pages else context.new_page()
            focus_login_window(context, ("Microsoft Edge",))
            page.goto("https://jwxt.hue.edu.cn/jsxsd/", wait_until="commit", timeout=20000)
            focus_login_window(context, ("教务", "教学", "Microsoft Edge"))
            with _state_lock:
                cancelled = ticket not in _jwxt_login_state or cancel_event.is_set()
                if not cancelled:
                    _jwxt_login_state[ticket].update(state="waiting", message="请在弹出的教务系统官网完成登录")
            if cancelled:
                context.close()
                context = None
                return
            person = None
            for _ in range(120):
                if cancel_event.is_set():
                    context.close()
                    context = None
                    raise RuntimeError("登录已取消")
                if all(tab.is_closed() for tab in context.pages):
                    raise RuntimeError("教务系统窗口已关闭")
                for tab in context.pages:
                    host = (urlparse(tab.url).hostname or "").lower() if not tab.is_closed() else ""
                    if host == "jwxt.hue.edu.cn" and "/jsxsd/" in urlparse(tab.url).path:
                        for frame in tab.frames:
                            if urlparse(frame.url).hostname == "jwxt.hue.edu.cn":
                                person = jwxt_adapter.identity(frame.content())
                                if person:
                                    break
                        if person:
                            break
                if not person and _ % 5 == 0:
                    try:
                        response = context.request.get("https://jwxt.hue.edu.cn/jsxsd/framework/xsMain.jsp", timeout=10000)
                        if response.ok and response.url.startswith("https://jwxt.hue.edu.cn/jsxsd/framework/xsMain.jsp"):
                            person = jwxt_adapter.identity(response.text())
                    except Exception:
                        pass
                if person:
                    break
                time.sleep(2)
            context.storage_state(path=str(PROFILES / folder_name / "storage_state.json"))
            context.close()
            context = None
        if not person:
            raise RuntimeError("未从教务首页识别学生身份，请确认登录后能看到个人中心")
        if cancel_event.is_set():
            return
        digest = jwxt_student_hash(person["number"])
        with db() as conn:
            owner = conn.execute("SELECT id FROM users WHERE jwxt_student_hash=?", (digest,)).fetchone()
            if bind_user_id:
                if owner and owner["id"] != bind_user_id:
                    raise RuntimeError("该教务账号已关联另一学习空间，请先使用原账号登录")
                current = conn.execute("SELECT jwxt_student_hash FROM users WHERE id=?", (bind_user_id,)).fetchone()
                if not current or (current["jwxt_student_hash"] and current["jwxt_student_hash"] != digest):
                    raise RuntimeError("当前学习空间已关联其他教务账号")
                user_id = bind_user_id
                conn.execute("UPDATE users SET jwxt_student_hash=?,jwxt_profile_dir=? WHERE id=?",
                             (digest, folder_name, user_id))
            elif owner:
                user_id = owner["id"]
                conn.execute("UPDATE users SET jwxt_profile_dir=? WHERE id=?", (folder_name, user_id))
            else:
                cur = conn.execute("""INSERT INTO users
                    (username,password_hash,created_at,display_name,jwxt_student_hash,jwxt_profile_dir)
                    VALUES (?,?,?,?,?,?)""", ("jwxt_" + digest[:24], "!external-login", now(),
                                          person["name"], digest, folder_name))
                user_id = cur.lastrowid
        with _state_lock:
            if cancel_event.is_set() or ticket not in _jwxt_login_state:
                return
            _jwxt_sync_state.pop(user_id, None)
            _jwxt_login_state[ticket].update(state="success", message="教务系统登录成功", user_id=user_id)
    except Exception as exc:
        with _state_lock:
            if ticket in _jwxt_login_state:
                _jwxt_login_state[ticket].update(state="error", message=f"教务系统登录未完成：{str(exc)[:150]}")
    finally:
        if context:
            try:
                context.close()
            except Exception:
                pass


@app.post("/api/jwxt/login/start")
def start_jwxt_login(request: Request, force: bool = False, session: str | None = Cookie(default=None)):
    origin = request.headers.get("origin")
    if origin and origin not in ("http://127.0.0.1:8000", "http://localhost:8000"):
        raise HTTPException(403, "登录请求来源不匹配")
    bind_user_id = require_user(session) if session else None
    with _state_lock:
        other_ticket = reusable_login(_login_state, bind_user_id, False)
        if other_ticket and not force:
            raise HTTPException(409, "已有学习通登录窗口，请先完成、取消或重新打开")
        if other_ticket:
            reusable_login(_login_state, bind_user_id, True)
        active_ticket = reusable_login(_jwxt_login_state, bind_user_id, force)
        if active_ticket:
            return {"ticket": active_ticket, "resumed": True}
        ticket = secrets.token_hex(16)
        _jwxt_login_state[ticket] = new_login_state("正在打开教务系统", bind_user_id)
    threading.Thread(target=jwxt_login_browser, args=(ticket,), daemon=True).start()
    return {"ticket": ticket}


@app.get("/api/jwxt/login/status/{ticket}")
def jwxt_login_status(ticket: str, response: Response):
    status = login_status(_jwxt_login_state, ticket)
    if status["state"] == "success":
        make_session(response, status["user_id"])
    elapsed = int(time.monotonic() - status.get("started_monotonic", time.monotonic()))
    return {"state": status["state"], "message": status["message"], "elapsed": max(0, elapsed)}


@app.post("/api/jwxt/login/cancel/{ticket}")
def cancel_jwxt_login(ticket: str):
    return cancel_login(_jwxt_login_state, ticket)


def sync_jwxt_browser(uid: int):
    with _state_lock:
        _jwxt_sync_state[uid] = {"state": "syncing", "message": "正在只读查询课表与成绩"}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            profile = jwxt_profile_path(uid)
            browser, context = open_saved_browser_context(p, profile)
            page = context.new_page()
            page.goto("https://jwxt.hue.edu.cn/jsxsd/framework/xsMain.jsp",
                      wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1200)
            person = None
            current_week = None
            for frame in page.frames:
                if urlparse(frame.url).hostname == "jwxt.hue.edu.cn":
                    frame_html = frame.content()
                    person = person or jwxt_adapter.identity(frame_html)
                    current_week = current_week or jwxt_adapter.current_week(frame_html)
            with db() as conn:
                row = conn.execute("SELECT jwxt_student_hash FROM users WHERE id=?", (uid,)).fetchone()
            if not person or not row or jwxt_student_hash(person["number"]) != row["jwxt_student_hash"]:
                raise RuntimeError("教务会话已过期或账号已切换，请重新登录")
            schedule_url = "https://jwxt.hue.edu.cn/jsxsd/xskb/xskb_list.do"
            grades_url = "https://jwxt.hue.edu.cn/jsxsd/kscj/cjcx_list"
            schedule_response = context.request.get(schedule_url + "?Ves632DSdyV=NEW_XSD_PYGL", timeout=30000)
            grades_response = context.request.get(grades_url, timeout=30000)
            timetable = jwxt_adapter.schedule(schedule_response.text()) if schedule_response.ok and schedule_response.url.startswith(schedule_url) else []
            scores = jwxt_adapter.grades(grades_response.text()) if grades_response.ok and grades_response.url.startswith(grades_url) else []
            if not scores:
                grades_response = context.request.post(grades_url, data={}, timeout=30000)
                if grades_response.ok and grades_response.url.startswith(grades_url):
                    scores = jwxt_adapter.grades(grades_response.text())
            close_saved_browser_context(browser, context, profile)
        if timetable or scores:
            with db() as conn:
                save_academic_cache(conn, uid, timetable, scores, current_week)
        with _state_lock:
            _jwxt_sync_state[uid] = {"state": "done" if timetable or scores else "needs_attention",
                                     "message": f"读取 {len(timetable)} 条课表、{len(scores)} 条成绩" if timetable or scores
                                                else "官网页面结构尚未适配；未读取到课表或成绩，请以教务官网为准",
                                     "updated_at": now()}
    except Exception as exc:
        with _state_lock:
            _jwxt_sync_state[uid] = {"state": "needs_reauth" if "教务会话已过期" in str(exc) else "needs_attention",
                                     "message": f"教务读取失败：{str(exc)[:150]}",
                                     "updated_at": now()}


@app.post("/api/jwxt/sync")
def sync_jwxt(session: str | None = Cookie(default=None)):
    uid = require_user(session)
    if not browser_profile_ready(jwxt_profile_path(uid)):
        raise HTTPException(409, "请先使用教务系统登录")
    if not queue_sync(uid, _jwxt_sync_state, sync_jwxt_browser, "正在读取课表和成绩", force=True):
        raise HTTPException(409, "教务数据正在读取")
    return {"ok": True}


def sync_browser(uid: int):
    set_state(uid, "syncing", "正在读取课程和作业，请稍候")
    try:
        with db() as conn:
            academic = load_academic_cache(conn, uid)
        semester_names = list(dict.fromkeys(
            item["course"] for item in valid_semester_schedule(academic["schedule"]) if item.get("course")
        ))
        if not semester_names:
            raise RuntimeError("尚未取得本学期课程范围，请先同步教务系统课表")
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            profile = profile_path(uid)
            browser, context = open_saved_browser_context(p, profile)
            identity = ensure_profile_matches(context, uid)
            page = context.new_page()
            courses = []
            if identity["fid"].isdigit():
                course_url = f"https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata?courseType=1&courseFid={identity['fid']}"
                response = context.request.get(course_url, timeout=30000)
                if response.ok:
                    courses = parse_chaoxing_courses(response.text())
            if not courses:
                page.goto("https://i.mooc.chaoxing.com/space/index", wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1000)
                course_anchors = page.locator("a[href]").evaluate_all("els => els.map(a => ({name: (a.innerText || a.title || '').trim(), href: a.href})).filter(x => x.name && /course|clazz|interaction|stu/i.test(x.href))")
                for item in course_anchors:
                    name = item["name"].strip()
                    if 2 <= len(name) <= 100 and name not in ("我的课程", "我学的课"):
                        courses.append(item)
            page.goto("https://mooc1-api.chaoxing.com/mooc-ans/mooc2/work/all-task", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1800)
            if "login" in page.url.lower() or "passport" in page.url.lower():
                raise RuntimeError("登录会话已失效，请重新连接")
            tasks = []
            for _ in range(20):
                page_tasks = parse_all_task(page.content())
                known = {item["href"] for item in tasks}
                tasks.extend(item for item in page_tasks if item["href"] not in known)
                next_button = page.locator(".xl-nextPage:not(.xl-disabled)")
                if not page_tasks or next_button.count() == 0:
                    break
                first_href = page_tasks[0]["href"]
                next_button.first.click()
                try:
                    page.wait_for_function("previous => document.querySelector('.task-list li[data]')?.getAttribute('data') !== previous",
                                           arg=first_href, timeout=5000)
                except Exception:
                    break
            courses = [item for item in courses if belongs_to_current_semester(item.get("name", ""), semester_names)]
            tasks = [item for item in tasks if belongs_to_current_semester(item.get("course", ""), semester_names)]
            for item in tasks[:100]:
                href = urljoin(page.url, item["href"])
                try:
                    detail = context.request.get(href, timeout=20000)
                    if detail.ok and chaoxing_url(detail.url):
                        published_at, due_at = parse_chaoxing_answer_window(detail.text())
                        item["published_at"] = published_at
                        item["due_at"] = due_at or item["due_at"]
                    time.sleep(0.15)
                except Exception:
                    continue
            close_saved_browser_context(browser, context, profile)
        added = 0
        with db() as conn:
            normalize_chaoxing_assignments(conn, uid)
            for item in courses:
                ensure_course(conn, uid, item["name"], external_id=item.get("external_id", item["href"]), url=item["href"])
            for item in tasks:
                href = urljoin("https://mooc1-api.chaoxing.com/mooc-ans/mooc2/work/all-task", item["href"])
                if not chaoxing_url(href):
                    continue
                external_id = chaoxing_assignment_key(href)
                cid = ensure_course(conn, uid, item["course"])
                existing = conn.execute("SELECT id,due_at,published_at,platform_status FROM assignments WHERE user_id=? AND external_id=?",
                                        (uid, external_id)).fetchone()
                if existing:
                    changed_status = item["platform_status"] != existing["platform_status"]
                    changed_deadline = item["due_at"] and item["due_at"] != existing["due_at"]
                    conn.execute("""UPDATE assignments SET course_id=?,title=?,description=?,source_url=?,
                        due_at=COALESCE(?,due_at),published_at=COALESCE(?,published_at),
                        platform_status=?,updated_at=? WHERE id=?""",
                        (cid, item["title"], item["context"], href, item["due_at"], item["published_at"],
                         item["platform_status"], now(), existing["id"]))
                    if changed_deadline:
                        add_event(conn, uid, "deadline", f"截止时间变化：{item['title']}", existing["id"])
                    if changed_status:
                        label = "已完成" if item["platform_status"] == "completed" else "待完成"
                        add_event(conn, uid, "platform_status", f"学习通状态更新为{label}：{item['title']}", existing["id"])
                    continue
                cur = conn.execute("""INSERT INTO assignments
                  (user_id,course_id,external_id,title,description,due_at,published_at,platform_status,
                   source_url,source,created_at,updated_at)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (uid, cid, external_id, item["title"], item["context"], item["due_at"],
                  item["published_at"], item["platform_status"], href, "chaoxing", now(), now()))
                add_event(conn, uid, "discovered", f"发现新作业：{item['title']}", cur.lastrowid)
                added += 1
        set_state(uid, "done", f"本学期同步完成：发现 {added} 项新作业，匹配 {len(courses)} 门学习通课程")
    except Exception as exc:
        expired = "登录已过期" in str(exc) or "登录会话已失效" in str(exc)
        set_state(uid, "needs_reauth" if expired else "needs_attention", f"同步失败：{str(exc)[:180]}")


@app.post("/api/chaoxing/sync")
def sync(session: str | None = Cookie(default=None)):
    uid = require_user(session)
    if not browser_profile_ready(profile_path(uid)):
        raise HTTPException(409, "请重新使用学习通登录")
    if not queue_sync(uid, _sync_state, sync_browser, "正在读取课程和作业", force=True):
        raise HTTPException(409, "操作正在进行")
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/static/{name}")
def static_file(name: str):
    if name not in ("app.js", "style.css"):
        raise HTTPException(404)
    return FileResponse(ROOT / "static" / name)
