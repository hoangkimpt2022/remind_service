#!/usr/bin/env python3
# remind_service_full.py
# Full runnable service: Notion + Telegram + Scheduler
# Requirements:
#   pip install flask requests python-dateutil pytz apscheduler

import os
import requests
import time
import datetime
from dateutil import parser as dateparser
from dateutil.relativedelta import relativedelta
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request, jsonify

app = Flask(__name__)

# ---------------- CONFIG (env or defaults you requested) ----------------
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip()
REMIND_DB = os.getenv("REMIND_NOTION_DATABASE", "").strip()
GOALS_DB = os.getenv("GOALS_NOTION_DATABASE", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
SELF_URL = os.getenv("SELF_URL", "").strip()

TIMEZONE = os.getenv("TIMEZONE", "Asia/Ho_Chi_Minh")
TZ = pytz.timezone(TIMEZONE)

# Daily reminder time default 14:00 per request
REMIND_HOUR = int(os.getenv("REMIND_HOUR", "14"))
REMIND_MINUTE = int(os.getenv("REMIND_MINUTE", "0"))
WEEKLY_HOUR = int(os.getenv("WEEKLY_HOUR", "20"))
MONTHLY_HOUR = int(os.getenv("MONTHLY_HOUR", "08"))
RUN_ON_START = os.getenv("RUN_ON_START", "true").lower() in ("1", "true", "yes")

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}" if NOTION_TOKEN else "",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json"
}

# ---------- PROPERTY NAMES (defaults provided per user's spec) ----------
PROP_TITLE = os.getenv("PROP_TITLE", "Aa name")
PROP_DONE = os.getenv("PROP_DONE", "Done")
PROP_ACTIVE = os.getenv("PROP_ACTIVE", "active")
PROP_DUE = os.getenv("PROP_DUE", "Ngày cần làm")
PROP_COMPLETED = os.getenv("PROP_COMPLETED", "Ngày hoàn thành thực tế")
PROP_REL_GOAL = os.getenv("PROP_REL_GOAL", "Related Mục tiêu")
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

# Cache for /check -> /done mapping
LAST_TASKS = []

# ---------------- Notion helpers ----------------
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
    if not db_id:
        return []
    payload = {"page_size": page_size}
    if filter_payload:
        payload["filter"] = filter_payload
    try:
        res = req_post(f"/databases/{db_id}/query", payload)
        return res.get("results", [])
    except Exception as e:
        print("Notion query error:", e)
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
# ================== THÊM HÀM NÀY VÀO – BẮT BUỘC PHẢI CÓ ==================
def send_telegram(text):
    """
    Hàm gửi tin nhắn Telegram cơ bản.
    Được gọi bởi job_daily, /check, /done, /new, v.v.
    Đây là hàm bị thiếu trong file gốc của bạn!
    """
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

# (Sau đó để nguyên hàm send_telegram_long của bạn)
def send_telegram_long(text):
    """
    Gửi tin nhắn dài > 4096 ký tự bằng cách chia nhỏ
    """
    max_len = 3800
    parts = [text[i:i+max_len] for i in range(0, len(text), max_len)]
    for part in parts:
        send_telegram(part)
        time.sleep(0.5)  # tránh bị rate limit

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

# ---------------- Goal property reader (robust) ----------------
def read_goal_properties(goal_page):
    props = goal_page.get("properties", {})

    def safe_select(k):
        v = props.get(k, {})
        sel = v.get("select")
        if sel and isinstance(sel, dict):
            return sel.get("name")
        return None

    def safe_date(k):
        v = props.get(k, {})
        raw = v.get("date", {}).get("start")
        if raw:
            try:
                return dateparser.parse(raw).date()
            except:
                return None
        return None

    def safe_formula(k):
        v = props.get(k, {})
        f = v.get("formula")
        if f:
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

    def safe_rollup_number(k):
        v = props.get(k, {})
        ru = v.get("rollup")
        if ru:
            if "number" in ru and ru.get("number") is not None:
                return ru.get("number")
            arr = ru.get("array")
            if isinstance(arr, list):
                return len(arr)
        return None

    def safe_text(k):
        v = props.get(k, {})
        rt = v.get("rich_text", [])
        if rt:
            return "".join([t.get("plain_text","") for t in rt])
        if "title" in v and v.get("title"):
            return "".join([t.get("plain_text","") for t in v.get("title",[])])
        return None

    out = {}
    out["id"] = goal_page.get("id")
    out["title"] = get_title(goal_page)
    out["trang_thai"] = safe_select(GOAL_PROP_STATUS)
    out["ngay_bat_dau"] = safe_date(GOAL_PROP_START)
    out["ngay_hoan_thanh"] = safe_date(GOAL_PROP_END)
    out["dem_nguoc_formula"] = safe_formula(GOAL_PROP_COUNTDOWN)
    out["tien_do_formula"] = safe_formula(GOAL_PROP_PROGRESS)
    out["tong_nhiem_vu_rollup"] = safe_rollup_number(GOAL_PROP_TOTAL_TASKS)
    out["nhiem_vu_da_hoan_rollup"] = safe_rollup_number(GOAL_PROP_DONE_TASKS)
    out["nhiem_vu_con_lai_formula"] = safe_formula(GOAL_PROP_REMAIN)
    out["nhiem_vu_hoan_tuan_rollup"] = safe_rollup_number(GOAL_PROP_DONE_WEEK)
    out["nhiem_vu_hoan_thang_rollup"] = safe_rollup_number(GOAL_PROP_DONE_MONTH)
    # computed days remaining if dem_nguoc absent
    out["days_remaining_computed"] = None
    if out["dem_nguoc_formula"] is None and out["ngay_hoan_thanh"]:
        try:
            today = datetime.datetime.now(TZ).date()
            out["days_remaining_computed"] = (out["ngay_hoan_thanh"] - today).days
        except:
            out["days_remaining_computed"] = None
    return out

# ---------------- Build task text ----------------
def format_task_line(i, page):
    title = get_title(page)
    pri = get_select_name(page, PROP_PRIORITY) or ""
    delta = overdue_days(page)
    if delta is None:
        symbol = "🟡"
        note = ""
    else:
        if delta > 0:
            symbol = "🔴"
            note = f"↳ Đã trễ {delta} ngày, làm ngay đi sếp ơi!"
        elif delta == 0:
            symbol = "🟡"
            note = "↳💥Làm Ngay Hôm nay!"
        else:
            symbol = "🟢"
            note = ""
    return f"{i} {symbol} <b>{title}</b> — Cấp độ: {pri}\n  {note}".rstrip()

# ---------------- Jobs (daily / weekly / monthly) ----------------
def job_daily():
    now = datetime.datetime.now(TZ)
    today = now.date()
    start_week, end_week = week_range(today)

    # Query tasks: not done & due this week or before today
    filters = [
        {"property": PROP_DONE, "checkbox": {"equals": False}},
        {"or": [
            {"property": PROP_DUE, "date": {"on_or_after": start_week.isoformat(), "on_or_before": end_week.isoformat()}},
            {"property": PROP_DUE, "date": {"before": today.isoformat()}}
        ]}
    ]
    if PROP_ACTIVE:
        filters.insert(0, {"property": PROP_ACTIVE, "checkbox": {"equals": True}})

    tasks = notion_query(REMIND_DB, {"and": filters})
    weekly_tasks = []
    for p in tasks:
        # include tasks from filter; if you want only "Hằng ngày" add check on PROP_TYPE
        weekly_tasks.append(p)

    # Build message header and task lines
    lines = [f"🔔 <b>Hôm nay {today.strftime('%d/%m/%Y')} sếp có {len(weekly_tasks)} nhiệm vụ hằng ngày</b>", ""]
    for i, p in enumerate(weekly_tasks, start=1):
        lines.append(format_task_line(i, p))

    # Goals: display goals with due tasks and progress/countdown
    goal_lines = []
    total_goal_tasks_due = 0
    if GOALS_DB:
        goals = notion_query(GOALS_DB)
        for g in goals:
            ginfo = read_goal_properties(g)
            # countdown text preference
            if ginfo.get("dem_nguoc_formula"):
                countdown_text = str(ginfo["dem_nguoc_formula"])
            elif ginfo.get("days_remaining_computed") is not None:
                d = ginfo["days_remaining_computed"]
                if d > 0:
                    countdown_text = f"còn {d} ngày"
                elif d == 0:
                    countdown_text = "hết hạn hôm nay"
                else:
                    countdown_text = f"đã trễ {-d} ngày"
            else:
                countdown_text = "không có thông tin ngày hoàn thành"

            # progress: prefer formula, else compute from rollups
            pct = None
            done = None; total = None
            if ginfo.get("tien_do_formula") is not None:
                try:
                    pct = int(float(ginfo.get("tien_do_formula")))
                except:
                    pct = None
            elif ginfo.get("tong_nhiem_vu_rollup") is not None and ginfo.get("nhiem_vu_da_hoan_rollup") is not None:
                total = ginfo["tong_nhiem_vu_rollup"]
                done = ginfo["nhiem_vu_da_hoan_rollup"]
                try:
                    pct = round(done / total * 100) if total and total > 0 else 0
                except:
                    pct = 0

            # related tasks due/overdue
            related_tasks = notion_query(REMIND_DB, {"filter": {"property": PROP_REL_GOAL, "relation": {"contains": g.get("id")}}}) if PROP_REL_GOAL else []
            relevant = []
            for p in related_tasks:
                d = overdue_days(p)
                if d is not None and d >= 0:
                    relevant.append((p, d))
            if relevant:
                total_goal_tasks_due += len(relevant)
                goal_lines.append(f"🔗 Mục tiêu: <b>{ginfo['title']}</b> — {countdown_text}")
                # progress line with bar
                if pct is not None:
                    bar = render_progress_bar(pct)
                    if done is not None and total is not None:
                        goal_lines.append(f"   → Tiến độ: {pct}% ({done}/{total}) {bar}")
                    else:
                        goal_lines.append(f"   → Tiến độ: {pct}% {bar}")
                else:
                    goal_lines.append(f"   → Tiến độ: không có dữ liệu")
                for p, d in relevant:
                    t = get_title(p)
                    pri = get_select_name(p, PROP_PRIORITY) or ""
                    note = f"↳🔴Đã trễ {d} ngày, làm ngay đi sếp ơi!" if d>0 else "↳💥Làm Ngay Hôm nay!"
                    sym = "🔴" if d>0 else "🟡"
                    goal_lines.append(f"   - {sym} {t} — Cấp độ: {pri}\n     {note}")

    if total_goal_tasks_due:
        lines.append("")
        lines.append(f"🔗 sếp có {total_goal_tasks_due} nhiệm vụ Mục tiêu")
        lines.extend(goal_lines)

    send_telegram("\n".join(lines).strip())

    # Cache LAST_TASKS for /done
    global LAST_TASKS
    LAST_TASKS = [p.get("id") for p in weekly_tasks]

def job_weekly():
    now = datetime.datetime.now(TZ).date()
    start_week, end_week = week_range(now)

    # Completed this week
    filters = [
        {"property": PROP_DONE, "checkbox": {"equals": True}},
        {"property": PROP_COMPLETED, "date": {"on_or_after": start_week.isoformat(), "on_or_before": end_week.isoformat()}}
    ]
    done_this_week = notion_query(REMIND_DB, {"and": filters})
    daily_done = sum(1 for p in done_this_week if "hằng" in (get_select_name(p, PROP_TYPE).lower() if get_select_name(p, PROP_TYPE) else ""))
    overdue_done = 0
    for p in done_this_week:
        due = get_date_start(p, PROP_DUE)
        comp = get_date_start(p, PROP_COMPLETED)
        if due and comp and comp.date() > due.date():
            overdue_done += 1

    # Overdue not done
    filters2 = [
        {"property": PROP_DONE, "checkbox": {"equals": False}},
        {"property": PROP_DUE, "date": {"before": datetime.datetime.now(TZ).date().isoformat()}}
    ]
    if PROP_ACTIVE:
        filters2.insert(0, {"property": PROP_ACTIVE, "checkbox": {"equals": True}})
    q2 = notion_query(REMIND_DB, {"and": filters2})
    overdue_remaining = len(q2)

    # Goals summary (uses rollups/formula if exist)
    goals_summary = []
    if GOALS_DB:
        goals = notion_query(GOALS_DB)
        for g in goals:
            ginfo = read_goal_properties(g)
            total = ginfo.get("tong_nhiem_vu_rollup")
            done_total = ginfo.get("nhiem_vu_da_hoan_rollup")
            weekly_done = ginfo.get("nhiem_vu_hoan_tuan_rollup")
            progress_pct = None
            if ginfo.get("tien_do_formula") is not None:
                try:
                    progress_pct = int(float(ginfo["tien_do_formula"]))
                except:
                    progress_pct = None
            elif total is not None and done_total is not None:
                try:
                    progress_pct = round(done_total / total * 100) if total and total>0 else 0
                except:
                    progress_pct = 0
            if total is not None:
                goals_summary.append({"name": ginfo["title"], "progress": progress_pct or 0, "done": done_total or 0, "total": total or 0, "weekly_done": weekly_done or 0})

    # Build weekly message
    lines = [f"📊 <b>Báo cáo tuần — {datetime.datetime.now(TZ).date().strftime('%d/%m/%Y')}</b>", ""]
    lines.append("🔥 <b>Công việc hằng ngày</b>")
    lines.append(f"• ✔ Hoàn thành: {daily_done}")
    lines.append(f"• ⏳ Quá hạn đã hoàn thành: {overdue_done}")
    lines.append(f"• 🆘 Quá hạn chưa làm: {overdue_remaining}")
    lines.append("")
    lines.append("🎯 <b>Mục tiêu nổi bật</b>")
    for g in sorted(goals_summary, key=lambda x: -x['progress'])[:6]:
        bar = render_progress_bar(g['progress'])
        lines.append(f"• {g['name']}")
        lines.append(f"  → Tiến độ: {g['progress']}% ({g['done']}/{g['total']}) {bar}")
        lines.append(f"  → Nhiệm vụ hoàn thành tuần này: {g['weekly_done']}")
    lines.append("")
    lines.append("📈 <b>Tổng quan</b>")
    lines.append("Sếp đang tiến rất tốt! hãy lăn quả cùa tuyết này để tiến tới hoàn thành mục tiêu lớn. 🎯 Tuần sau bứt phá thêm nhé! 🔥🔥🔥")
    send_telegram("\n".join(lines))

def job_monthly():
    now = datetime.datetime.now(TZ).date()
    mstart, mend = month_range(now)
    filters = [
        {"property": PROP_DONE, "checkbox": {"equals": True}},
        {"property": PROP_COMPLETED, "date": {"on_or_after": mstart.isoformat(), "on_or_before": mend.isoformat()}}
    ]
    done_this_month = notion_query(REMIND_DB, {"and": filters})
    daily_month_done = sum(1 for p in done_this_month if "hằng" in (get_select_name(p, PROP_TYPE).lower() if get_select_name(p, PROP_TYPE) else ""))
    goals_summary = []
    if GOALS_DB:
        goals = notion_query(GOALS_DB)
        for g in goals:
            ginfo = read_goal_properties(g)
            total = ginfo.get("tong_nhiem_vu_rollup")
            done = ginfo.get("nhiem_vu_da_hoan_rollup")
            progress_pct = None
            if ginfo.get("tien_do_formula") is not None:
                try:
                    progress_pct = int(float(ginfo["tien_do_formula"]))
                except:
                    progress_pct = None
            elif total is not None and done is not None:
                try:
                    progress_pct = round(done / total * 100) if total and total>0 else 0
                except:
                    progress_pct = 0
            if total is not None:
                goals_summary.append({"name": ginfo["title"], "progress": progress_pct or 0, "done": done or 0, "total": total or 0})
    lines = [f"📅 <b>Báo cáo tháng {now.strftime('%m/%Y')}</b>", ""]
    lines.append(f"• ✔ Việc hằng ngày hoàn thành tháng: {daily_month_done}")
    lines.append("")
    lines.append("🎯 Tiến độ mục tiêu chính:")
    for g in sorted(goals_summary, key=lambda x: -x['progress'])[:6]:
        bar = render_progress_bar(g['progress'])
        lines.append(f"• {g['name']} → {g['progress']}% ({g['done']}/{g['total']}) {bar}")
    send_telegram("\n".join(lines))

# ---------------- Telegram webhook handlers ----------------
@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.json or {}
    message = update.get("message", {}) or {}
    chat_id = str(message.get("chat", {}).get("id", ""))
    if TELEGRAM_CHAT_ID and chat_id != TELEGRAM_CHAT_ID:
        return jsonify({"ok": False}), 403
    text = (message.get("text", "") or "").strip()
    if not text.startswith("/"):
        return jsonify({"ok": True}), 200

    # /check : show tasks for this week (and overdue)
    if text.lower() == "/check":
        now = datetime.datetime.now(TZ).date()
        start_week, end_week = week_range(now)
        filters = [
            {"property": PROP_DONE, "checkbox": {"equals": False}},
            {"or": [
                {"property": PROP_DUE, "date": {"on_or_after": start_week.isoformat(), "on_or_before": end_week.isoformat()}},
                {"property": PROP_DUE, "date": {"before": now.isoformat()}}
            ]}
        ]
        if PROP_ACTIVE:
            filters.insert(0, {"property": PROP_ACTIVE, "checkbox": {"equals": True}})
        tasks = notion_query(REMIND_DB, {"and": filters})
        if not tasks:
            send_telegram("🎉 Không có nhiệm vụ trong tuần này hoặc quá hạn để hiển thị.")
            return jsonify({"ok": True}), 200
        lines = [f"🔔 <b>Danh sách nhiệm vụ tuần {start_week.strftime('%d/%m')} - {end_week.strftime('%d/%m')}</b>", ""]
        for i, p in enumerate(tasks, start=1):
            lines.append(format_task_line(i, p))
        global LAST_TASKS
        LAST_TASKS = [p.get("id") for p in tasks]
        send_telegram("\n".join(lines))
        return jsonify({"ok": True}), 200

    # /done.<n>
    elif text.lower().startswith("/done."):
        try:
            # khai báo global phải nằm trước mọi sử dụng/ghép gán
            global LAST_TASKS

            parts = text.split(".", 1)
            n = int(parts[1])
            # đảm bảo LAST_TASKS đã tồn tại (module-level), nếu không, gán mặc định là []
            if 'LAST_TASKS' not in globals() or LAST_TASKS is None:
                LAST_TASKS = []

            if 1 <= n <= len(LAST_TASKS):
                page_id = LAST_TASKS[n - 1]
                now_iso = datetime.datetime.now(TZ).isoformat()
                props = {}
                # set Done checkbox
                props[PROP_DONE] = {"checkbox": True}
                # set completed date property if present
                if PROP_COMPLETED:
                    props[PROP_COMPLETED] = {"date": {"start": now_iso}}
                # update Notion page
                notion_update_page(page_id, props)

                # try fetch page for title (best-effort)
                title = ""
                try:
                    p = req_get(f"/pages/{page_id}")
                    title = get_title(p)
                except Exception:
                    title = ""

                send_telegram(f"✅ Đã đánh dấu Done cho nhiệm vụ số {n}. {title}")
            else:
                send_telegram("❌ Số không hợp lệ. Gõ /check để xem danh sách nhiệm vụ tuần này.")
        except ValueError:
            # parts[1] không phải số
            send_telegram("❌ Số không hợp lệ. Gõ /done.<số> (ví dụ /done.1).")
        except Exception as e:
            print("Error /done:", e)
            send_telegram("❌ Lỗi xử lý /done. Hãy dùng /done.<số> (ví dụ /done.1).")
        return jsonify({"ok": True}), 200

    # /new.<name>.<DDMMYY>.<HHMM>.<priority>
    elif text.lower().startswith("/new."):
        payload = text[5:]
        parts = payload.split(".")
        if len(parts) < 2:
            send_telegram("❌ Format sai! Ví dụ: /new.Gọi khách 150tr.081225.0900.cao")
            return jsonify({"ok": True}), 200
        name = parts[0].strip()
        date_part = parts[1].strip()
        time_part = parts[2].strip() if len(parts) >= 3 else "0000"
        priority = parts[3].strip().lower() if len(parts) >= 4 else "thấp"
        # parse date
        try:
            if len(date_part) == 6:
                dd = int(date_part[0:2]); mm = int(date_part[2:4]); yy = int(date_part[4:6]); yyyy = 2000 + yy
            elif len(date_part) == 8:
                dd = int(date_part[0:2]); mm = int(date_part[2:4]); yyyy = int(date_part[4:8])
            else:
                raise ValueError("Bad date")
            hh = int(time_part[0:2]) if len(time_part) >= 2 else 0
            mi = int(time_part[2:4]) if len(time_part) >= 4 else 0
            dt = datetime.datetime(yyyy, mm, dd, hh, mi)
            iso_due = TZ.localize(dt).isoformat()
        except Exception:
            send_telegram("❌ Không parse được ngày/giờ. Format ví dụ: DDMMYY (081225) và HHMM (0900).")
            return jsonify({"ok": True}), 200

        props = {}
        props[PROP_TITLE] = {"title": [{"text": {"content": name}}]}
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

@app.route("/debug/schema", methods=["GET"])
def debug_schema():
    """
    Trả về properties của REMIND DB để bạn kiểm tra tên cột chính xác.
    Truy cập: https://<your-app>/debug/schema
    """
    if not REMIND_DB:
        return jsonify({"error": "REMIND_NOTION_DATABASE not set"}), 400
    try:
        db = req_get(f"/databases/{REMIND_DB}")
        # trả về chỉ properties (an toàn)
        return jsonify({"database_id": REMIND_DB, "properties": db.get("properties", {})})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

# ---------------- Scheduler ----------------
def start_scheduler():
    sched = BackgroundScheduler(timezone=TIMEZONE)
    sched.add_job(job_daily, 'cron', hour=REMIND_HOUR, minute=REMIND_MINUTE, id='daily')
    sched.add_job(job_weekly, 'cron', day_of_week='sun', hour=WEEKLY_HOUR, minute=0, id='weekly')
    def monthly_wrapper():
        today = datetime.datetime.now(TZ).date()
        tomorrow = today + datetime.timedelta(days=1)
        if tomorrow.day == 1:
            job_monthly()
    sched.add_job(monthly_wrapper, 'cron', hour=MONTHLY_HOUR, minute=0, id='monthly')
    sched.start()
    print(f"Scheduler started: daily at {REMIND_HOUR:02d}:{REMIND_MINUTE:02d} ({TIMEZONE})")

def set_telegram_webhook():
    if TELEGRAM_TOKEN and WEBHOOK_URL:
        try:
            r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook", data={"url": WEBHOOK_URL}, timeout=10)
            print("setWebhook response:", r.text)
        except Exception as e:
            print("Error setting webhook:", e)

# ---------------- Main ----------------
if __name__ == "__main__":
    # Bắt lỗi cấu hình sớm
    if not NOTION_TOKEN or not REMIND_DB:
        print("FATAL: NOTION_TOKEN or REMIND_NOTION_DATABASE not set. Exiting.")
        raise SystemExit(1)

    # Đảm bảo HEADERS có Authorization (nếu chưa set ở khai báo trên)
    if "Authorization" not in HEADERS and NOTION_TOKEN:
        HEADERS["Authorization"] = f"Bearer {NOTION_TOKEN}"

    # Info
    print("Notion configured:", bool(NOTION_TOKEN), REMIND_DB[:8] + "..." if REMIND_DB else "")
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        print("Telegram configured: chat_id present.")
    else:
        print("Telegram NOT fully configured. Messages will be printed to console.")

    # Nếu muốn set webhook (chỉ khi WEBHOOK_URL set)
    if TELEGRAM_TOKEN and WEBHOOK_URL:
        set_telegram_webhook()
    else:
        if WEBHOOK_URL:
            print("WEBHOOK_URL set but TELEGRAM_TOKEN missing.")

    # Start scheduler
    start_scheduler()

    # RUN_ON_START sẽ chạy job_daily một lần khi khởi động (useful for testing)
    if RUN_ON_START:
        try:
            print("RUN_ON_START -> running job_daily() once at startup.")
            job_daily()
        except Exception as e:
            print("Error running job_daily on start:", e)
            # Thông báo bot đã khởi động thành công (rất quan trọng để biết deploy OK)
        try:
            startup_msg = f"""
        Bot nhắc việc đã KHỞI ĐỘNG THÀNH CÔNG!

        Thời gian: {datetime.datetime.now(TZ).strftime('%d/%m/%Y %H:%M')}
        Múi giờ: {TIMEZONE}
        Daily job: {REMIND_HOUR:02d}:{REMIND_MINUTE:02d}
        Hôm nay sẽ nhắc lúc {REMIND_HOUR}:00 nếu có việc
        """
            send_telegram(startup_msg.strip())
            print("Đã gửi tin nhắn khởi động tới Telegram!")
        except:
            print("Không gửi được tin nhắn khởi động (có thể do Telegram chưa config)")
    # Decide run mode: Background worker (no Flask) or Webhook (Flask)
    BACKGROUND_WORKER = os.getenv("BACKGROUND_WORKER", "true").lower() in ("1", "true", "yes")
    if BACKGROUND_WORKER:
        print("Running in BACKGROUND_WORKER mode (no Flask server). Process will stay alive for Render Worker.")
        try:
            # keep process alive (Render Background Worker expects process to keep running)
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("Shutting down.")
    else:
        # Run Flask to accept Telegram webhook calls
        port = int(os.getenv("PORT", 5000))
        print(f"Starting Flask server on port {port} for webhook mode.")
        app.run(host="0.0.0.0", port=port, threaded=True)
