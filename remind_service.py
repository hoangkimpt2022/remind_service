#!/usr/bin/env python3
# remind_service_full.py (modified with debug schema + notion_query logging)
# Requirements: pip install flask requests python-dateutil pytz apscheduler

import os
import requests
import time
import datetime
from dateutil import parser as dateparser
from dateutil.relativedelta import relativedelta
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request, jsonify
import openai
import json
from tenacity import retry, stop_after_attempt, wait_fixed

app = Flask(__name__)

# ---------------- CONFIG (env or defaults you requested) ----------------
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip()
REMIND_DB = os.getenv("REMIND_NOTION_DATABASE", "").strip()
GOALS_DB = os.getenv("GOALS_NOTION_DATABASE", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
SELF_URL = os.getenv("SELF_URL", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

TIMEZONE = os.getenv("TIMEZONE", "Asia/Ho_Chi_Minh")
TZ = pytz.timezone(TIMEZONE)

# Daily reminder time default 14:00 per request
REMIND_HOUR = int(os.getenv("REMIND_HOUR", "14"))
REMIND_MINUTE = int(os.getenv("REMIND_MINUTE", "0"))
WEEKLY_HOUR = int(os.getenv("WEEKLY_HOUR", "20"))
MONTHLY_HOUR = int(os.getenv("MONTHLY_HOUR", "08"))
RUN_ON_START = os.getenv("RUN_ON_START", "true").lower() in ("1", "true", "yes")

HEADERS = {
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json"
}
if NOTION_TOKEN:
    HEADERS["Authorization"] = f"Bearer {NOTION_TOKEN}"

# ---------- PROPERTY NAMES (defaults provided per user's spec) ----------
PROP_TITLE = os.getenv("PROP_TITLE", "Aa name")
PROP_DONE = os.getenv("PROP_DONE", "Done")
PROP_ACTIVE = os.getenv("PROP_ACTIVE", "").strip()    # keep empty by default
PROP_DUE = os.getenv("PROP_DUE", "Ngày cần làm")
PROP_COMPLETED = os.getenv("PROP_COMPLETED", "Ngày hoàn thành thực tế")
# single canonical PROP_REL_GOAL (no duplicate definitions)
PROP_REL_GOAL = os.getenv("PROP_REL_GOAL", "Related Mục tiêu").strip()
PROP_TYPE = os.getenv("PROP_TYPE", "Loại công việc")
PROP_PRIORITY = os.getenv("PROP_PRIORITY", "Cấp độ")
PROP_NOTE = os.getenv("PROP_NOTE", "note")

# Goals DB property names assumed (user-provided)
GOAL_PROP_STATUS = "Trạng thái"
GOAL_PROP_START = "Ngày bắt đầu"
GOAL_PROP_END = "Ngày hoàn thành"
GOAL_PROP_COUNTDOWN = "Đếm ngược"
GOAL_PROP_PROGRESS = "Tiến Độ"
GOAL_PROP_TOTAL_TASKS = "Tổng nhiệm vụ cần làm"
GOAL_PROP_DONE_TASKS = "Nhiệm vụ đã hoàn thành"
GOAL_PROP_REMAIN = "Nhiệm vụ còn lại"
GOAL_PROP_DONE_WEEK = "Nhiệm vụ hoàn thành tuần này"
GOAL_PROP_DONE_MONTH = "Nhiệm vụ hoàn thành tháng này"

print("ENV CHECK → GOALS_NOTION_DATABASE =", GOALS_DB)
print("ENV CHECK → PROP_REL_GOAL =", PROP_REL_GOAL)
print("ENV CHECK → REMIND_NOTION_DATABASE =", REMIND_DB)

# Cache for /check -> /done mapping
LAST_TASKS = []

# ---------------- Notion helpers (with debug) ----------------
def format_dt(dt_obj):
    """Format aware datetime/date -> 'DD/MM/YYYY' or 'DD/MM/YYYY HH:MM'."""
    if not dt_obj:
        return ""
    # if it's date object
    if isinstance(dt_obj, datetime.date) and not isinstance(dt_obj, datetime.datetime):
        return dt_obj.strftime("%d/%m/%Y")
    # datetime: ensure timezone aware -> convert to TZ
    try:
        if dt_obj.tzinfo is None:
            # assume naive is local TZ
            dt = TZ.localize(dt_obj)
        else:
            dt = dt_obj.astimezone(TZ)
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        try:
            return dt_obj.strftime("%d/%m/%Y %H:%M")
        except:
            return str(dt_obj)

def req_get(path):
    url = f"https://api.notion.com/v1{path}"
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()

def req_post(path, json_payload):
    url = f"https://api.notion.com/v1{path}"
    r = requests.post(url, headers=HEADERS, json=json_payload, timeout=20)
    r.raise_for_status()
    return r.json()

def req_patch(path, json_payload):
    url = f"https://api.notion.com/v1{path}"
    r = requests.patch(url, headers=HEADERS, json=json_payload, timeout=20)
    r.raise_for_status()
    return r.json()

def notion_query(db_id, filter_payload=None, page_size=100):
    """
    Debuggable notion query: prints payload + response body on non-200.
    Returns results list or [].
    """
    if not db_id:
        return []
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    payload = {"page_size": page_size}
    if filter_payload:
        # The code previously passed {"and": filters} or {"filter": {...}}
        # Here we accept filter_payload either as full body or as a 'filter' dict
        if isinstance(filter_payload, dict):
            # If user passed payload already containing 'and' or 'filter', use as-is
            # else assume it's the 'filter' to attach
            if "and" in filter_payload or "filter" in filter_payload or "or" in filter_payload:
                payload.update({"filter": filter_payload} if "filter" not in filter_payload else filter_payload)
            else:
                payload["filter"] = filter_payload
        else:
            # unlikely
            payload["filter"] = filter_payload

    try:
        r = requests.post(url, headers=HEADERS, json=payload, timeout=20)
        if r.status_code != 200:
            # print helpful debug
            try:
                payload_preview = str(payload)
            except:
                payload_preview = "<failed to serialize payload>"
            print("=== Notion query error (non-200) ===")
            print("Status:", r.status_code)
            print("Response body:", r.text[:2000])
            print("Payload sent:", payload_preview[:2000])
            print("Database id:", db_id)
            return []
        return r.json().get("results", [])
    except Exception as e:
        print("Exception in notion_query:", e)
        return []

def notion_create_page(db_id, properties):
    try:
        return req_post("/pages", {"parent": {"database_id": db_id}, "properties": properties})
    except Exception as e:
        print("Notion create error:", e)
        return None

def notion_update_page(page_id, properties):
    try:
        return req_patch(f"/pages/{page_id}", {"properties": properties})
    except Exception as e:
        print("Notion update error:", e)
        return None

# ---------------- Utility helpers ----------------
def get_title(page):
    p = page.get("properties", {}).get(PROP_TITLE)
    if p and p.get("type") == "title":
        return "".join([t.get("plain_text", "") for t in p.get("title", [])])
    # fallback search
    for v in page.get("properties", {}).values():
        if v.get("type") == "title":
            return "".join([t.get("plain_text", "") for t in v.get("title", [])])
    return "Untitled"

def get_note_text(page):
    if not PROP_NOTE:
        return ""
    prop = page.get("properties", {}).get(PROP_NOTE, {})
    if prop.get("type") == "rich_text":
        return extract_plain_text_from_rich_text(prop.get("rich_text", []))
    return ""

def get_checkbox(page, prop_name):
    if not prop_name:
        return False
    return bool(page.get("properties", {}).get(prop_name, {}).get("checkbox", False))

def get_select_name(page, prop_name):
    if not prop_name:
        return ""
    val = page.get("properties", {}).get(prop_name, {})
    sel = val.get("select")
    if sel and isinstance(sel, dict):
        return sel.get("name", "")
    return ""

def get_date_start(page, prop_name):
    if not prop_name:
        return None
    raw = page.get("properties", {}).get(prop_name, {}).get("date", {}).get("start")
    if not raw:
        return None
    try:
        return dateparser.parse(raw)
    except:
        return None

def get_relation_ids(page, prop_name):
    if not prop_name:
        return []
    rels = page.get("properties", {}).get(prop_name, {}).get("relation", []) or []
    return [r.get("id") for r in rels if r.get("id")]

def overdue_days(page):
    due_dt = get_date_start(page, PROP_DUE)
    if not due_dt:
        return None
    today = datetime.datetime.now(TZ).date()
    try:
        return (today - due_dt.date()).days
    except:
        return None

def week_range(date_obj):
    start = date_obj - datetime.timedelta(days=date_obj.weekday())
    end = start + datetime.timedelta(days=6)
    return start, end

def month_range(date_obj):
    first = date_obj.replace(day=1)
    last = (first + relativedelta(months=1) - datetime.timedelta(days=1))
    return first, last

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram Disabled] Message would be sent:\n", text)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        response = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=10
        )
        if response.status_code == 200:
            return True
        else:
            print(f"Telegram lỗi {response.status_code}: {response.text}")
            return False
    except Exception as e:
        print("Telegram gửi thất bại:", e)
        return False

def send_telegram_long(text):
    max_len = 3800
    parts = [text[i:i+max_len] for i in range(0, len(text), max_len)]
    for part in parts:
        send_telegram(part)
        time.sleep(0.5)

# ---------------- Progress bar helper ----------------
def render_progress_bar(percent, length=18):
    try:
        pct = int(round(float(percent)))
    except:
        pct = 0
    pct = max(0, min(100, pct))
    filled_len = int(round(length * pct / 100))
    bar = "█" * filled_len + "-" * (length - filled_len)
    return f"[{bar}] {pct}%"

# ---------------- Goal helpers (robust & separated) ----------------
def extract_plain_text_from_rich_text(rich):
    if not rich:
        return ""
    return "".join(part.get("plain_text","") for part in rich)

def find_prop_key(props: dict, key_like: str):
    if not props or not key_like:
        return None
    if key_like in props:
        return key_like
    low = key_like.lower()
    for k in props.keys():
        if k.lower() == low:
            return k
    for k in props.keys():
        if low in k.lower():
            return k
    return None

def extract_prop_text(props: dict, key_like: str) -> str:
    if not props or not key_like:
        return ""
    k = find_prop_key(props, key_like)
    if not k:
        return ""
    prop = props.get(k, {}) or {}
    ptype = prop.get("type")
    if ptype == "formula":
        formula = prop.get("formula", {})
        if formula.get("string") is not None:
            return str(formula.get("string"))
        if formula.get("number") is not None:
            return str(formula.get("number"))
        if formula.get("boolean") is not None:
            return "1" if formula.get("boolean") else "0"
        if formula.get("date"):
            return formula["date"].get("start", "") or ""
        return ""
    if ptype == "rollup":
        roll = prop.get("rollup", {})
        if roll.get("number") is not None:
            return str(roll.get("number"))
        arr = roll.get("array") or []
        if arr:
            first = arr[0]
            if isinstance(first, dict):
                if first.get("id"):
                    return first.get("id")
                if "title" in first:
                    return extract_plain_text_from_rich_text(first.get("title", []))
                if "plain_text" in first:
                    return first.get("plain_text", "")
            return str(first)
        return ""
    if ptype == "title":
        return extract_plain_text_from_rich_text(prop.get("title", [])) or ""
    if ptype == "rich_text":
        return extract_plain_text_from_rich_text(prop.get("rich_text", [])) or ""
    if ptype == "number":
        return "" if prop.get("number") is None else str(prop.get("number"))
    if ptype == "date":
        d = prop.get("date", {}) or {}
        return d.get("start", "") or ""
    if ptype == "checkbox":
        return "1" if prop.get("checkbox") else "0"
    if ptype == "select":
        sel = prop.get("select") or {}
        return sel.get("name", "") or ""
    if ptype == "multi_select":
        arr = prop.get("multi_select") or []
        return ", ".join(a.get("name", "") for a in arr)
    if ptype == "relation":
        rel = prop.get("relation") or []
        if rel:
            return rel[0].get("id", "") or ""
    return ""

def safe_select(props: dict, name: str):
    k = find_prop_key(props, name)
    if not k:
        return None
    v = props.get(k, {})
    sel = v.get("select")
    if sel and isinstance(sel, dict):
        return sel.get("name")
    return None

def safe_date(props: dict, name: str):
    k = find_prop_key(props, name)
    if not k:
        return None
    raw = props.get(k, {}).get("date", {}).get("start")
    if not raw:
        return None
    try:
        return dateparser.parse(raw).date()
    except:
        return None

def safe_formula(props: dict, name: str):
    k = find_prop_key(props, name)
    if not k:
        return None
    f = props.get(k, {}).get("formula")
    if not f:
        return None
    if "string" in f and f.get("string") is not None:
        return f.get("string")
    if "number" in f and f.get("number") is not None:
        return f.get("number")
    if "date" in f and f.get("date") is not None:
        try:
            return dateparser.parse(f.get("date").get("start")).date()
        except:
            return None
    return None

def safe_rollup_number(props: dict, name: str):
    k = find_prop_key(props, name)
    if not k:
        return None
    ru = props.get(k, {}).get("rollup")
    if not ru:
        return None
    if ru.get("number") is not None:
        return ru.get("number")
    arr = ru.get("array") or []
    return len(arr) if isinstance(arr, list) else None

def read_goal_properties(goal_page):
    """
    Robust reader for goal page properties.
    Accepts None, returns a dict with keys used by job logic.
    Normalizes progress into progress_pct (int 0..100) if possible.
    """
    out = {
        "id": "",
        "title": "(no title)",
        "trang_thai": None,
        "ngay_bat_dau": None,
        "ngay_hoan_thanh": None,
        "dem_nguoc_formula": None,
        "tien_do_formula": None,
        "tong_nhiem_vu_rollup": None,
        "nhiem_vu_da_hoan_rollup": None,
        "nhiem_vu_con_lai_formula": None,
        "nhiem_vu_hoan_tuan_rollup": None,
        "nhiem_vu_hoan_thang_rollup": None,
        "days_remaining_computed": None,
        "progress_pct": None
    }

    if not goal_page or not isinstance(goal_page, dict):
        return out

    props = goal_page.get("properties", {}) or {}
    out["id"] = goal_page.get("id", "") or ""
    # find title
    title = ""
    for k, v in props.items():
        if v.get("type") == "title":
            title = extract_plain_text_from_rich_text(v.get("title", []))
            break
    out["title"] = title or get_title(goal_page) or out["id"]

    # safe extraction using existing helpers
    out["trang_thai"] = safe_select(props, GOAL_PROP_STATUS)
    out["ngay_bat_dau"] = safe_date(props, GOAL_PROP_START)
    out["ngay_hoan_thanh"] = safe_date(props, GOAL_PROP_END)
    out["dem_nguoc_formula"] = safe_formula(props, GOAL_PROP_COUNTDOWN)
    out["tien_do_formula"] = safe_formula(props, GOAL_PROP_PROGRESS)
    out["tong_nhiem_vu_rollup"] = safe_rollup_number(props, GOAL_PROP_TOTAL_TASKS)
    out["nhiem_vu_da_hoan_rollup"] = safe_rollup_number(props, GOAL_PROP_DONE_TASKS)
    out["nhiem_vu_con_lai_formula"] = safe_formula(props, GOAL_PROP_REMAIN)
    out["nhiem_vu_hoan_tuan_rollup"] = safe_rollup_number(props, GOAL_PROP_DONE_WEEK)
    out["nhiem_vu_hoan_thang_rollup"] = safe_rollup_number(props, GOAL_PROP_DONE_MONTH)

    # compute days_remaining_computed if formula missing but end date present
    if out.get("dem_nguoc_formula") is None and out.get("ngay_hoan_thanh"):
        try:
            today = datetime.datetime.now(TZ).date()
            out["days_remaining_computed"] = (out["ngay_hoan_thanh"] - today).days
        except Exception:
            out["days_remaining_computed"] = None

    # --------- Normalize progress (formula or rollup) into integer percent ---------
    progress_pct = None
    raw_prog = out.get("tien_do_formula")
    if raw_prog is not None:
        try:
            s = str(raw_prog).strip()
            if s.endswith("%"):
                s = s[:-1].strip()
            val = float(s)
            if val <= 1:
                val = val * 100
            progress_pct = int(round(val))
        except Exception:
            progress_pct = None

    # fallback when formula missing -> use rollups
    if progress_pct is None:
        try:
            total = out.get("tong_nhiem_vu_rollup")
            done = out.get("nhiem_vu_da_hoan_rollup")
            if total and (done is not None):
                progress_pct = int(round(float(done) / float(total) * 100)) if total > 0 else 0
        except Exception:
            progress_pct = None

    out["progress_pct"] = progress_pct

    return out

# ---------------- Build task text ----------------
def format_task_line(i, page):
    title = get_title(page)
    pri = get_select_name(page, PROP_PRIORITY) or ""
    icon = priority_icon(pri)

    # thời gian
    delta = overdue_days(page)
    if delta is None:
        note = ""
    elif delta > 0:
        note = f"↳⏰ Đã trễ {delta} ngày, làm ngay đi sếp ơi!"
    elif delta == 0:
        note = "↳💥 Làm Ngay Hôm nay!"
    else:
        note = f"↳⏳ Còn {abs(delta)} ngày nữa"

    return f"{i} {icon} <b>{title}</b> — Cấp độ: {pri}\n  {note}".rstrip()
# ---------------- Priority emoji helper ----------------
def priority_emoji(priority: str) -> str:
    if not priority:
        return "🟡"
    p = priority.strip().lower()
    if p == "cao":
        return "🔴"
    if p in ("tb", "trung bình"):
        return "🟡"
    if p == "thấp":
        return "🟢"
    return "🟡"

# ---------------- Jobs (daily / weekly / monthly) ----------------
def job_daily():
    now = datetime.datetime.now(TZ)
    today = datetime.datetime.now(TZ).date()

    print("[INFO] job_daily start, today =", today.isoformat())

    # Query tasks for "hôm nay" (not done & due today)
    # --------------------------------------------------
    # Query tasks for daily reminder (by priority logic)
    # --------------------------------------------------
    filters = [
        {"property": PROP_DONE, "checkbox": {"equals": False}},
        {"property": PROP_DUE, "date": {"is_not_empty": True}}
    ]
    if PROP_ACTIVE:
        filters.insert(0, {"property": PROP_ACTIVE, "checkbox": {"equals": True}})

    try:
        all_tasks = notion_query(REMIND_DB, {"and": filters}) or []
        print(f"[DBG] fetched {len(all_tasks)} active tasks with due date")
    except Exception as e:
        print("[WARN] job_daily: notion_query REMIND_DB failed:", e)
        all_tasks = []

    tasks = []
    today = datetime.datetime.now(TZ).date()

    for p in all_tasks:
        try:
            due_dt = get_date_start(p, PROP_DUE)
            if not due_dt:
                continue

            due_date = due_dt.date()
            days_left = (due_date - today).days
            pri = (get_select_name(p, PROP_PRIORITY) or "").lower()

            # ===== PRIORITY RULE =====
            if pri == "cao" and days_left <= 2:
                tasks.append(p)
            elif pri in ("tb", "trung bình") and days_left <= 1:
                tasks.append(p)
            elif pri == "thấp" and days_left <= 0:
                tasks.append(p)

        except Exception as e:
            print("[WARN] skipping task in priority filter:", e)
            continue

    print(f"[DBG] daily reminder tasks after priority filter: {len(tasks)}")


    # --------------------------------------------------
    # Build header and task lines (daily reminder result)
    # --------------------------------------------------
    lines = [
        f"🔔 <b>Hôm nay {today.strftime('%d/%m/%Y')} sếp có {len(tasks)} nhiệm vụ hằng ngày</b>",
        ""
    ]

    for i, p in enumerate(tasks, start=1):
        try:
            if not p or not isinstance(p, dict):
                continue
            if get_checkbox(p, PROP_DONE):
                continue

            title = get_title(p)
            pri = get_select_name(p, PROP_PRIORITY) or ""
            sym = priority_emoji(pri)

            note_text = get_note_text(p)
            due_dt = get_date_start(p, PROP_DUE)
            due_text = f" — hạn: {format_dt(due_dt)}" if due_dt else ""

            # system note (NO EMOJI HERE)
            d = overdue_days(p)
            if d is None:
                sys_note = ""
            elif d > 0:
                sys_note = f"↳⏰ Đã trễ {d} ngày, làm ngay đi sếp ơi!"
            elif d == 0:
                sys_note = "↳💥Làm Ngay Hôm nay!"
            else:
                sys_note = f"↳⏳ Còn {abs(d)} ngày nữa"

            line = f"{i} {sym} <b>{title}</b> — Cấp độ: {pri}{due_text}"

            if note_text:
                line += f"\n📝 {note_text}"
            if sys_note:
                line += f"\n  {sys_note}"

            lines.append(line)

        except Exception as ex:
            print("[ERROR] formatting daily task:", ex)
            continue
    # --------------------------------------------------
    # Goals section – ONLY from tasks already selected
    # --------------------------------------------------
    goal_map = {}

    for p in tasks:
        rels = (
            p.get("properties", {})
            .get(PROP_REL_GOAL, {})
            .get("relation", [])
        )
        for r in rels:
            gid = r.get("id")
            if gid:
                goal_map.setdefault(gid, []).append(p)

    goal_lines = []
    total_goal_tasks_due = 0

    if GOALS_DB and goal_map:
        goals = notion_query(GOALS_DB) or []

        for g in goals:
            gid = g.get("id")
            if gid not in goal_map:
                continue

            ginfo = read_goal_properties(g)
            related_tasks = goal_map[gid]
            total_goal_tasks_due += len(related_tasks)

            header = f"🎯 Mục tiêu: <b>{ginfo.get('title') or gid}</b> — "

            if ginfo.get("dem_nguoc_formula") is not None:
                header += str(ginfo["dem_nguoc_formula"])
            elif ginfo.get("days_remaining_computed") is not None:
                drem = ginfo["days_remaining_computed"]
                header += (
                    f"còn {drem} ngày"
                    if drem > 0
                    else "hết hạn hôm nay"
                    if drem == 0
                    else f"đã trễ {-drem} ngày"
                )
            else:
                header += "không có thông tin ngày hoàn thành"

            if ginfo.get("ngay_bat_dau"):
                header += f" — bắt đầu: {format_dt(ginfo['ngay_bat_dau'])}"

            goal_lines.append(header)

            pct = ginfo.get("progress_pct")
            if pct is not None:
                goal_lines.append(f"   → Tiến độ: {pct}% {render_progress_bar(pct)}")
            else:
                goal_lines.append("   → Tiến độ: không có dữ liệu")

            for p in related_tasks:
                title = get_title(p)
                pri = get_select_name(p, PROP_PRIORITY) or ""
                sym = priority_emoji(pri)

                due_dt = get_date_start(p, PROP_DUE)
                due_text = f" — hạn: {format_dt(due_dt)}" if due_dt else ""

                d = overdue_days(p)
                if d is None:
                    sys_note = ""
                elif d > 0:
                    sys_note = f"↳ Đã trễ {d} ngày"
                elif d == 0:
                    sys_note = "↳💥Làm Ngay Hôm nay!"
                else:
                    sys_note = f"↳Còn {abs(d)} ngày nữa"

                line = f"   - {sym} <b>{title}</b> — Cấp độ: {pri}{due_text}"

                nt = get_note_text(p)
                if nt:
                    line += f"\n     📝 {nt}"
                if sys_note:
                    line += f"\n     {sys_note}"

                goal_lines.append(line)

    if total_goal_tasks_due:
        lines.append("")
        lines.append(f"🎯 sếp có {total_goal_tasks_due} nhiệm vụ Mục tiêu")
        lines.extend(goal_lines)
    send_telegram("\n".join(lines).strip())

    global LAST_TASKS
    LAST_TASKS = [p.get("id") for p in tasks if p and isinstance(p, dict)]

    # ================== AI PLANNING – MENTOR MODE ==================
    if GOALS_DB and OPENAI_API_KEY:
        openai.api_key = OPENAI_API_KEY
        goals = notion_query(GOALS_DB) or []
        ai_plan_summaries = []

        # ---------- GPT CALL ----------
        @retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
        def call_gpt(messages):
            resp = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=messages,
                temperature=0.25,
                max_tokens=500
            )
            return resp.choices[0].message.content.strip()

        # ---------- WORKLOAD SNAPSHOT ----------
        today = datetime.now(TZ).date()
        week_start = today - datetime.timedelta(days=today.weekday())
        week_end = week_start + datetime.timedelta(days=13)

        current_tasks = notion_query(
            REMIND_DB,
            {
                "and": [
                    {"property": PROP_DONE, "checkbox": {"equals": False}},
                    {"property": PROP_DUE, "date": {"on_or_after": week_start.isoformat()}},
                    {"property": PROP_DUE, "date": {"on_or_before": week_end.isoformat()}}
                ]
            }
        ) or []

        tasks_by_day = {}
        overdue_count = 0
        high_priority_count = 0
        task_titles = []

        for t in current_tasks:
            due_dt = get_date_start(t, PROP_DUE)
            if not due_dt:
                continue

            d = due_dt.date()
            key = d.strftime("%d/%m")
            tasks_by_day[key] = tasks_by_day.get(key, 0) + 1

            pri = get_select_name(t, PROP_PRIORITY) or "TB"
            if pri == "Cao":
                high_priority_count += 1
            if d < today:
                overdue_count += 1

            task_titles.append(get_title(t).lower())

        avg_tasks_per_day = (
            sum(tasks_by_day.values()) / max(len(tasks_by_day), 1)
            if tasks_by_day else 0
        )

        # ---------- AI GATE (QUYẾT ĐỊNH Ở CODE) ----------
        if overdue_count >= 2:
            lines.append("🤖 AI tạm dừng: đang có task quá hạn – ưu tiên dọn backlog.")
            return

        if high_priority_count >= 3:
            lines.append("🤖 AI tạm dừng: quá nhiều task ưu tiên Cao.")
            return

        if avg_tasks_per_day > 5:
            lines.append("🤖 AI tạm dừng: tải công việc tuần này quá cao.")
            return

        # ---------- SCHEDULE SUMMARY ----------
        schedule_summary = "\n".join(
            [f"- {d}: {c} task" for d, c in sorted(tasks_by_day.items())]
        ) or "Lịch hiện tại khá trống."

        # ---------- PER GOAL ----------
        for goal in goals:
            ginfo = read_goal_properties(goal)

            if ginfo.get("progress_pct", 0) >= 100 or ginfo.get("trang_thai") in ("Done", "Hoàn thành"):
                continue

            # ----- SYSTEM + USER PROMPT -----
            system_msg = {
                "role": "system",
                "content": (
                    "You are a world-class personal planning mentor. "
                    "You think in 80/20, critical path, and deep work. "
                    "Return VALID JSON ONLY."
                )
            }

            user_msg = {
                "role": "user",
                "content": f"""
    Bạn là chuyên gia lập kế hoạch cá nhân cấp cao nhất thế giới
    (James Clear + Cal Newport + Greg McKeown).

    LỊCH 2 TUẦN TỚI:
    {schedule_summary}

    TẢI CÔNG VIỆC:
    - Task quá hạn: {overdue_count}
    - Task ưu tiên Cao: {high_priority_count}
    - Trung bình task/ngày: {avg_tasks_per_day:.1f}

    MỤC TIÊU:
    - Tên: {ginfo['title']}
    - Tiến độ: {ginfo.get('progress_pct', 0)}%
    - Deadline: {format_dt(ginfo.get('ngay_hoan_thanh')) or "Chưa rõ"}
    - Còn lại: {ginfo.get('days_remaining_computed', 'không rõ')} ngày

    NHIỆM VỤ:
    1. Xác định CRITICAL BOTTLENECK
    2. Quyết định CÓ NÊN tạo task tuần này không
    3. Nếu có → tối đa 2 task, impact cao – effort thấp
    4. Task làm được trong 30–60 phút
    5. Deadline trong 7 ngày tới
    6. Nếu không nên tạo → tasks = []

    FORMAT JSON:
    {{
    "goal": "{ginfo['title']}",
    "critical_bottleneck": "...",
    "tasks": [
        {{
        "name": "...",
        "due": "YYYY-MM-DD",
        "priority": "Cao/TB/Thấp",
        "expected_outcome": "...",
        "best_time": "Sáng sớm/Tối muộn/Linh hoạt",
        "note": "Lý do 80/20 + mẹo làm nhanh"
        }}
    ],
    "summary": "Vì sao tạo hoặc không tạo task"
    }}
    """
            }

            try:
                raw = call_gpt([system_msg, user_msg])
                plan = json.loads(raw)

                created = 0

                for t in plan.get("tasks", []):
                    name = t["name"].strip()
                    if name.lower() in task_titles:
                        continue

                    due = t["due"]
                    if not due:
                        continue

                    props = {
                        PROP_TITLE: {"title": [{"text": {"content": name}}]},
                        PROP_DUE: {"date": {"start": due}},
                        PROP_PRIORITY: {"select": {"name": t.get("priority", "TB")}},
                        PROP_NOTE: {"rich_text": [{"text": {"content": t.get("note", "")}}]},
                        PROP_DONE: {"checkbox": False},
                        PROP_REL_GOAL: {"relation": [{"id": goal["id"]}]}
                    }

                    if PROP_ACTIVE:
                        props[PROP_ACTIVE] = {"checkbox": True}

                    if notion_create_page(REMIND_DB, props):
                        created += 1

                ai_plan_summaries.append(
                    f"🎯 <b>{plan['goal']}</b>\n"
                    f"→ AI tạo <b>{created}</b> task\n"
                    f"🧠 {plan.get('critical_bottleneck','')}\n"
                    f"💭 {plan.get('summary','')}"
                )

            except Exception as e:
                print(f"[AI ERROR] {ginfo['title']}: {e}")

        if ai_plan_summaries:
            lines.append("")
            lines.append("🤖 <b>AI ĐIỀU PHỐI CÔNG VIỆC TUẦN NÀY</b>")
            lines.extend(ai_plan_summaries)
            lines.append("\nTask mới đã vào Notion – dùng /check để xem.")

    # ================== END AI PLANNING ==================


def _parse_completed_datetime_from_page(page):
    """
    Try multiple ways to get completed datetime from a page:
    1) If PROP_COMPLETED is a date property, get_date_start will return datetime.
    2) If it's a formula/rollup returning a date-like string, extract_prop_text gives the string -> try parse.
    Returns datetime or None.
    """
    # 1) direct date property
    dt = get_date_start(page, PROP_COMPLETED)
    if dt:
        return dt

    # 2) fallback: extract text from any property (handles formula/rollup string)
    try:
        props = page.get("properties", {}) or {}
        s = extract_prop_text(props, PROP_COMPLETED)
        if s:
            # try parse flexible with dateutil
            try:
                parsed = dateparser.parse(s)
                return parsed
            except Exception:
                return None
    except Exception:
        return None
    return None

def job_monthly():
    """
    Robust monthly report:
    - Count items completed during current month (using parsed completed datetime).
    - Count daily items completed in month (type contains 'hằng').
    - Build goals summary (progress, done/total, monthly_done).
    - Send Telegram message.
    """
    today = datetime.datetime.now(TZ).date()
    mstart, mend = month_range(today)  # first day and last day (date)
    print(f"[INFO] job_monthly start for {mstart} -> {mend}")

    # Fetch pages marked Done (don't filter by completed date on Notion side)
    filters_done = [{"property": PROP_DONE, "checkbox": {"equals": True}}]
    if PROP_ACTIVE:
        filters_done.insert(0, {"property": PROP_ACTIVE, "checkbox": {"equals": True}})

    try:
        done_pages = notion_query(REMIND_DB, {"and": filters_done})
        print(f"[DBG] job_monthly: fetched {len(done_pages)} done pages")
    except Exception as e:
        print("[WARN] job_monthly: notion_query for done_pages failed:", e)
        done_pages = []

    # compute done_this_month by parsing completed datetime
    done_this_month = []
    for p in done_pages:
        try:
            comp_dt = _parse_completed_datetime_from_page(p)
            if comp_dt is None:
                continue
            comp_date = comp_dt.date() if isinstance(comp_dt, datetime.datetime) else comp_dt
            if comp_date >= mstart and comp_date <= mend:
                done_this_month.append((p, comp_date))
        except Exception as ex:
            print("[WARN] job_monthly: error parsing completed date for page", p.get("id"), ex)
            continue

    # total daily items completed in month (type contains 'hằng')
    daily_month_done = 0
    for p, comp_date in done_this_month:
        try:
            ttype = get_select_name(p, PROP_TYPE) or ""
            if "hằng" in ttype.lower():
                daily_month_done += 1
        except Exception:
            continue

    # overdue_done: among done_this_month, count where completed date > due date
    overdue_done = 0
    for p, comp_date in done_this_month:
        try:
            due = get_date_start(p, PROP_DUE)
            if due and comp_date and comp_date > due.date():
                overdue_done += 1
        except Exception:
            continue

    # Overdue not done: tasks not done and due before today (same as weekly)
    filters_overdue = [
        {"property": PROP_DONE, "checkbox": {"equals": False}},
        {"property": PROP_DUE, "date": {"before": datetime.datetime.now(TZ).date().isoformat()}}
    ]
    if PROP_ACTIVE:
        filters_overdue.insert(0, {"property": PROP_ACTIVE, "checkbox": {"equals": True}})
    try:
        q_overdue = notion_query(REMIND_DB, {"and": filters_overdue})
        overdue_remaining = len(q_overdue)
    except Exception as e:
        print("[WARN] job_monthly: notion_query overdue_remaining failed:", e)
        overdue_remaining = 0

    # Goals summary: use read_goal_properties; include monthly rollup if present
    goals_summary = []
    if GOALS_DB:
        try:
            goals = notion_query(GOALS_DB)
            print(f"[DBG] job_monthly: fetched {len(goals)} goals")
        except Exception as e:
            print("[WARN] job_monthly: notion_query GOALS_DB failed:", e)
            goals = []
        for g in goals:
            try:
                ginfo = read_goal_properties(g)
            except Exception as e:
                print("[WARN] job_monthly: read_goal_properties failed for goal:", g.get("id"), e)
                ginfo = {}

            total = ginfo.get("tong_nhiem_vu_rollup")
            done_total = ginfo.get("nhiem_vu_da_hoan_rollup")
            monthly_done = ginfo.get("nhiem_vu_hoan_thang_rollup") or 0

            # compute progress_pct using normalized field if present
            progress_pct = ginfo.get("progress_pct")
            if progress_pct is None:
                # fallback to formula raw or rollups
                raw = ginfo.get("tien_do_formula")
                if raw is not None:
                    try:
                        rp = str(raw).strip()
                        if rp.endswith("%"):
                            rp = rp[:-1].strip()
                        val = float(rp)
                        if val <= 1:
                            val = val * 100
                        progress_pct = int(round(val))
                    except:
                        progress_pct = None
                elif total and done_total is not None:
                    try:
                        progress_pct = int(round(float(done_total) / float(total) * 100)) if total and total > 0 else 0
                    except:
                        progress_pct = None

            # ensure ints
            gs = {
                "name": ginfo.get("title") or "(no title)",
                "progress": int(progress_pct) if progress_pct is not None else 0,
                "done": done_total or 0,
                "total": total or 0,
                "monthly_done": monthly_done or 0
            }
            goals_summary.append(gs)

    # Build monthly message
    lines = [f"📅 <b>Báo cáo tháng {today.strftime('%m/%Y')}</b>", ""]
    lines.append(f"• ✔ Việc hằng ngày hoàn thành tháng: {daily_month_done}")
    lines.append(f"• ⏳ Quá hạn đã hoàn thành: {overdue_done}")
    lines.append(f"• 🆘 Quá hạn chưa làm: {overdue_remaining}")
    lines.append("")
    lines.append("🎯 Tiến độ mục tiêu chính:")
    # sort by progress desc
    for g in sorted(goals_summary, key=lambda x: -x['progress'])[:8]:
        bar = render_progress_bar(g['progress'])
        lines.append(f"• {g['name']} → {g['progress']}% ({g['done']}/{g['total']}) {bar}")
        lines.append(f"  → Nhiệm vụ hoàn thành tháng này: {g['monthly_done']}")
    lines.append("")
    lines.append("📈 <b>Tổng quan</b>")
    lines.append("Sếp đang tiến rất tốt! Hãy lăn quả tuyết này để tiến tới hoàn thành mục tiêu lớn. 🎯 Tháng sau bứt phá thêm nhé! 🔥🔥🔥")

    send_telegram("\n".join(lines).strip())
    print(f"[INFO] job_monthly sent: daily_done={daily_month_done}, done_this_month={len(done_this_month)}, goals={len(goals_summary)}")

# ---------------- Telegram webhook handlers ----------------
@app.route("/webhook", methods=["POST"])
def webhook():
    global LAST_TASKS
    try:
        update = request.get_json(silent=True) or {}
        message = update.get("message", {}) or {}
        chat_id = str(message.get("chat", {}).get("id", ""))
        if TELEGRAM_CHAT_ID and chat_id != TELEGRAM_CHAT_ID:
            return jsonify({"ok": False, "error": "forbidden chat id"}), 403
        text = (message.get("text", "") or "").strip()
        if not text.startswith("/"):
            return jsonify({"ok": True}), 200
        cmd = text.strip()
        # /check : show tasks for this week (and overdue)
        if text.lower() == "/check":
            import traceback
            # ensure we declare global before any assignment in this function scope
            global LAST_TASKS
            try:
                now = datetime.datetime.now(TZ).date()
                start_week, end_week = week_range(now)

                # Build filters: not Done, due this week OR before today (overdue)
                filters = [
                    {"property": PROP_DONE, "checkbox": {"equals": False}},
                    {"or": [
                        {"property": PROP_DUE, "date": {"on_or_after": start_week.isoformat(), "on_or_before": end_week.isoformat()}},
                        {"property": PROP_DUE, "date": {"before": now.isoformat()}}
                    ]}
                ]
                if PROP_ACTIVE:
                    filters.insert(0, {"property": PROP_ACTIVE, "checkbox": {"equals": True}})

                # debug: print filters to logs
                print("[DBG] /check filters:", filters)

                tasks = notion_query(REMIND_DB, {"and": filters}) or []
                print(f"[DBG] /check got {len(tasks)} tasks from Notion")

                # If no tasks, respond early
                if not tasks:
                    send_telegram("🎉 Không có nhiệm vụ trong tuần này hoặc quá hạn để hiển thị.")
                    return jsonify({"ok": True}), 200

                # Build header: show week range
                lines = [f"🔔 <b>Danh sách nhiệm vụ tuần {start_week.strftime('%d/%m')} - {end_week.strftime('%d/%m')}</b>", ""]

                # For each task, format a line including due datetime and priority; skip any tasks that somehow are marked done
                visible_tasks = []
                for p in tasks:
                    try:
                        # defensive: ensure dict
                        if not p or not isinstance(p, dict):
                            print("[WARN] /check skipping invalid page object:", p)
                            continue
                        # skip if page is marked done (double-check)
                        try:
                            if get_checkbox(p, PROP_DONE):
                                print("[DBG] /check skipping task already done:", get_title(p))
                                continue
                        except Exception:
                            # if get_checkbox fails, continue but log
                            print("[WARN] get_checkbox failed for page", p.get("id"))

                        title = get_title(p)
                        pri = get_select_name(p, PROP_PRIORITY) or ""
                        sym = priority_emoji(pri)
                        note_text = get_note_text(p)
                        # due date/time
                        due_dt = None
                        try:
                            due_dt = get_date_start(p, PROP_DUE)
                        except Exception:
                            # fallback: try to extract as text
                            try:
                                due_text_raw = extract_prop_text(p.get("properties", {}) or {}, PROP_DUE)
                                # don't try to parse here; we'll just show raw if present
                                if due_text_raw:
                                    due_dt = due_text_raw
                            except Exception:
                                due_dt = None

                        due_text = f" — hạn: {format_dt(due_dt) if isinstance(due_dt, datetime.datetime) or isinstance(due_dt, datetime.date) else due_dt}" if due_dt else ""
                        # overdue/remaining note
                        note = ""
                        d = overdue_days(p)
                        if d is None:
                            sys_note = ""
                        elif d > 0:
                            sys_note = f"↳⏰ Đã trễ {d} ngày, làm ngay đi sếp ơi!"
                        elif d == 0:
                            sys_note = "↳💥 Làm ngay hôm nay!"
                        else:
                            sys_note = f"↳⏳ Còn {abs(d)} ngày nữa"

                        # append formatted line
                        line = f"{len(visible_tasks)+1} {sym} <b>{title}</b> — Cấp độ: {pri}{due_text}"

                        # note từ Notion (rich_text)
                        if note_text:
                            line += f"\n📝 {note_text}"

                        # note hệ thống (quá hạn / hôm nay / còn bao nhiêu ngày)
                        if note:
                            line += f"\n  {note}"

                        lines.append(line)

                    except Exception as e:
                        print("[ERROR] formatting /check task line:", e)
                        traceback.print_exc()
                        continue

                # cache LAST_TASKS for /done (only tasks we showed)
                try:
                    LAST_TASKS = [p.get("id") for p in visible_tasks if p and isinstance(p, dict)]
                    print("[DBG] /check LAST_TASKS set:", LAST_TASKS)
                except Exception as e:
                    print("[WARN] setting LAST_TASKS failed:", e)
                    traceback.print_exc()
                    LAST_TASKS = []

                # send message
                try:
                    send_telegram("\n".join(lines))
                except Exception as e:
                    print("[ERROR] send_telegram in /check failed:", e)
                    traceback.print_exc()
                    # still respond OK to webhook caller
                return jsonify({"ok": True}), 200

            except Exception as e:
                # print full traceback so we can see the root cause in logs
                print("Error handling /check:", e)
                traceback.print_exc()
                send_telegram("❌ Lỗi khi lấy danh sách nhiệm vụ. Vui lòng thử lại sau.")
                return jsonify({"ok": True}), 200


        # /done
        elif cmd.lower().startswith("/done."):
            if not isinstance(LAST_TASKS, list):
                LAST_TASKS = []
            parts = cmd.split(".", 1)
            if len(parts) < 2 or not parts[1].strip().isdigit():
                send_telegram("❌ Số không hợp lệ. Gõ /done.<số> (ví dụ /done.1).")
                return jsonify({"ok": True}), 200
            n = int(parts[1].strip())
            if n < 1 or n > len(LAST_TASKS):
                send_telegram("❌ Số không hợp lệ. Gõ /check để xem danh sách nhiệm vụ tuần này.")
                return jsonify({"ok": True}), 200
            page_id = LAST_TASKS[n - 1]
            now_iso = datetime.datetime.now(TZ).isoformat()
            props = {PROP_DONE: {"checkbox": True}}
            if PROP_COMPLETED:
                props[PROP_COMPLETED] = {"date": {"start": now_iso}}
            notion_update_page(page_id, props)
            title = ""
            try:
                p = req_get(f"/pages/{page_id}")
                title = get_title(p)
            except Exception:
                title = ""
            send_telegram(f"✅ Đã đánh dấu Done cho nhiệm vụ số {n}. {title}")
            return jsonify({"ok": True}), 200
        # /new
        elif cmd.lower().startswith("/new."):
            payload = cmd[5:]
            parts = payload.split(".")
            if len(parts) < 2:
                send_telegram("❌ Format sai! Ví dụ: /new.Gọi khách 150tr.081225.0900.cao")
                return jsonify({"ok": True}), 200
            name = parts[0].strip()
            date_part = parts[1].strip()
            time_part = parts[2].strip() if len(parts) >= 3 else "0000"
            priority = parts[3].strip().lower() if len(parts) >= 4 else "thấp"
            try:
                if len(date_part) == 6:
                    dd = int(date_part[0:2]); mm = int(date_part[2:4]); yy = int(date_part[4:6]); yyyy = 2000 + yy
                elif len(date_part) == 8:
                    dd = int(date_part[0:2]); mm = int(date_part[2:4]); yyyy = int(date_part[4:8])
                else:
                    raise ValueError("Bad date format")
                hh = int(time_part[0:2]) if len(time_part) >= 2 else 0
                mi = int(time_part[2:4]) if len(time_part) >= 4 else 0
                dt = datetime.datetime(yyyy, mm, dd, hh, mi)
                iso_due = TZ.localize(dt).isoformat()
            except Exception:
                send_telegram("❌ Không parse được ngày/giờ. Format ví dụ: DDMMYY (081225) và HHMM (0900).")
                return jsonify({"ok": True}), 200
            props = {PROP_TITLE: {"title": [{"text": {"content": name}}]}}
            if PROP_DUE:
                props[PROP_DUE] = {"date": {"start": iso_due}}
            if PROP_PRIORITY:
                props[PROP_PRIORITY] = {"select": {"name": priority.capitalize()}}
            if PROP_TYPE:
                props[PROP_TYPE] = {"select": {"name": "Hằng ngày"}}
            if PROP_ACTIVE:
                props[PROP_ACTIVE] = {"checkbox": True}
            if PROP_DONE:
                props[PROP_DONE] = {"checkbox": False}
            newp = notion_create_page(REMIND_DB, props)
            if newp:
                send_telegram(f"✅ Đã tạo nhiệm vụ: {name} — hạn: {dt.strftime('%d/%m/%Y %H:%M')} — cấp độ: {priority}")
            else:
                send_telegram("❌ Lỗi tạo nhiệm vụ. Kiểm tra token và database id.")
            return jsonify({"ok": True}), 200
        send_telegram("❓ Lệnh không nhận diện. Dùng /check, /done.<n>, /new.<tên>.<DDMMYY>.<HHMM>.<cấp độ>")
        return jsonify({"ok": True}), 200
    except Exception as e:
        print("Unhandled exception in webhook:", e)
        send_telegram("❌ Lỗi nội bộ khi xử lý lệnh. Vui lòng thử lại sau.")
        return jsonify({"ok": True}), 200

@app.route("/debug/schema", methods=["GET"])
def debug_schema():
    if not REMIND_DB:
        return jsonify({"error": "REMIND_NOTION_DATABASE not set"}), 400
    try:
        db = req_get(f"/databases/{REMIND_DB}")
        return jsonify({"database_id": REMIND_DB, "properties": db.get("properties", {})})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/health", methods=["GET"])
def health():
    return "OK", 200
    
@app.route("/wake", methods=["GET"])
def wake():
    return "ok", 200
    
# ---------------- Schema debug helper (print at startup) ----------------
def print_db_schema_once(db_id, label="DB"):
    try:
        r = requests.get(f"https://api.notion.com/v1/databases/{db_id}", headers=HEADERS, timeout=10)
        if r.status_code != 200:
            print(f"[DEBUG] GET /databases/{db_id} returned {r.status_code}: {r.text[:1000]}")
            return
        schema = r.json()
        props = schema.get("properties", {})
        print(f"[DEBUG] {label} properties keys ({len(props)}):")
        for k, v in props.items():
            print("  -", k, "(", v.get("type"), ")")
    except Exception as e:
        print("[DEBUG] print_db_schema_once error:", e)
# secure manual trigger endpoints for weekly/monthly reports
# place this near other Flask route definitions
MANUAL_TRIGGER_SECRET = os.getenv("MANUAL_TRIGGER_SECRET", "").strip()  # set in env to protect endpoints

@app.route("/debug/run_weekly", methods=["POST", "GET"])
def debug_run_weekly():
    # security: require secret if provided
    if MANUAL_TRIGGER_SECRET:
        token = request.args.get("token", "") or request.headers.get("X-Run-Token", "")
        if token != MANUAL_TRIGGER_SECRET:
            return jsonify({"error": "forbidden"}), 403
    try:
        job_weekly()
        return jsonify({"ok": True, "msg": "job_weekly executed"}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/debug/run_monthly", methods=["POST", "GET"])
def debug_run_monthly():
    if MANUAL_TRIGGER_SECRET:
        token = request.args.get("token", "") or request.headers.get("X-Run-Token", "")
        if token != MANUAL_TRIGGER_SECRET:
            return jsonify({"error": "forbidden"}), 403
    try:
        job_monthly()
        return jsonify({"ok": True, "msg": "job_monthly executed"}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ---------------- Scheduler ----------------
def start_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    sched = BackgroundScheduler(timezone=TIMEZONE)

    # daily
    try:
        if 'job_daily' in globals():
            sched.add_job(job_daily, 'cron', hour=REMIND_HOUR, minute=REMIND_MINUTE, id='daily')
        else:
            print("[WARN] job_daily not defined; skipping daily job.")
    except Exception as e:
        print("[ERROR] adding daily job:", e)

    # weekly
    try:
        if 'job_weekly' in globals():
            sched.add_job(job_weekly, 'cron', day_of_week='sun', hour=WEEKLY_HOUR, minute=0, id='weekly')
        else:
            print("[WARN] job_weekly not defined; skipping weekly job.")
    except Exception as e:
        print("[ERROR] adding weekly job:", e)

    # monthly wrapper
    def monthly_wrapper():
        today = datetime.datetime.now(TZ).date()
        tomorrow = today + datetime.timedelta(days=1)
        if tomorrow.day == 1:
            try:
                if 'job_monthly' in globals():
                    job_monthly()
                else:
                    print("[WARN] job_monthly not defined; skipping run.")
            except Exception as e:
                print("[ERROR] running job_monthly:", e)

    try:
        sched.add_job(monthly_wrapper, 'cron', hour=MONTHLY_HOUR, minute=0, id='monthly')
    except Exception as e:
        print("[ERROR] adding monthly wrapper job:", e)

    try:
        sched.start()
        print(f"[INFO] Scheduler started: daily at {REMIND_HOUR:02d}:{REMIND_MINUTE:02d} ({TIMEZONE})")
    except Exception as e:
        print("[ERROR] scheduler start failed:", e)

    return sched

def set_telegram_webhook():
    if TELEGRAM_TOKEN and WEBHOOK_URL:
        try:
            r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook", data={"url": WEBHOOK_URL}, timeout=10)
            try:
                print("setWebhook response:", r.status_code, r.json())
            except:
                print("setWebhook response:", r.status_code, r.text)
        except Exception as e:
            print("Error setting webhook:", e)

# ---------------- Main ----------------
if __name__ == "__main__":
    # early config check
    if not NOTION_TOKEN or not REMIND_DB:
        print("FATAL: NOTION_TOKEN or REMIND_NOTION_DATABASE not set. Exiting.")
        raise SystemExit(1)

    # ensure headers
    if "Authorization" not in HEADERS and NOTION_TOKEN:
        HEADERS["Authorization"] = f"Bearer {NOTION_TOKEN}"

    print("Notion configured:", bool(NOTION_TOKEN), REMIND_DB[:8] + "..." if REMIND_DB else "")
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        print("Telegram configured: chat_id present.")
    else:
        print("Telegram NOT fully configured. Messages will be printed to console.")

    # Print schema for both DBs to help debug property names
    try:
        if REMIND_DB:
            print_db_schema_once(REMIND_DB, label="REMIND_DB")
        if GOALS_DB:
            print_db_schema_once(GOALS_DB, label="GOALS_DB")
    except Exception as e:
        print("Startup schema debug error:", e)

    if TELEGRAM_TOKEN and WEBHOOK_URL:
        set_telegram_webhook()
    else:
        if WEBHOOK_URL:
            print("WEBHOOK_URL set but TELEGRAM_TOKEN missing.")

    start_scheduler()

    if RUN_ON_START:
        try:
            print("RUN_ON_START -> running job_daily() once at startup.")
            job_daily()
        except Exception as e:
            print("Error running job_daily on start:", e)

    BACKGROUND_WORKER = os.getenv("BACKGROUND_WORKER", "true").lower() in ("1", "true", "yes")
    if BACKGROUND_WORKER:
        print("Running in BACKGROUND_WORKER mode (no Flask server). Process will stay alive for Render Worker.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("Shutting down.")
    else:
        port = int(os.getenv("PORT", 5000))
        print(f"Starting Flask server on port {port} for webhook mode.")
        app.run(host="0.0.0.0", port=port, threaded=True)
