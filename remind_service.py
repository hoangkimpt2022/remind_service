#!/usr/bin/env python3
# remind_service_full.py - Enhanced with Deep AI Planning & Mentoring
# Requirements: pip install flask requests python-dateutil pytz apscheduler openai tenacity

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
from collections import defaultdict
from math import ceil

app = Flask(__name__)

# ============================================================================
# CONFIG
# ============================================================================
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip()
REMIND_DB = os.getenv("REMIND_NOTION_DATABASE", "").strip()
GOALS_DB = os.getenv("GOALS_NOTION_DATABASE", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
SELF_URL = os.getenv("SELF_URL", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
ENABLE_AI = os.getenv("ENABLE_AI", "true").lower() in ("1", "true", "yes")

TIMEZONE = os.getenv("TIMEZONE", "Asia/Ho_Chi_Minh")
TZ = pytz.timezone(TIMEZONE)

REMIND_HOUR = int(os.getenv("REMIND_HOUR", "14"))
REMIND_MINUTE = int(os.getenv("REMIND_MINUTE", "0"))
WEEKLY_HOUR = int(os.getenv("WEEKLY_HOUR", "20"))
MONTHLY_HOUR = int(os.getenv("MONTHLY_HOUR", "8"))
RUN_ON_START = os.getenv("RUN_ON_START", "true").lower() in ("1", "true", "yes")

HEADERS = {
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json"
}
if NOTION_TOKEN:
    HEADERS["Authorization"] = f"Bearer {NOTION_TOKEN}"

# Property names
PROP_TITLE = os.getenv("PROP_TITLE", "Aa name")
PROP_DONE = os.getenv("PROP_DONE", "Done")
PROP_ACTIVE = os.getenv("PROP_ACTIVE", "").strip()
PROP_DUE = os.getenv("PROP_DUE", "Ngày cần làm")
PROP_COMPLETED = os.getenv("PROP_COMPLETED", "Ngày hoàn thành thực tế")
PROP_REL_GOAL = os.getenv("PROP_REL_GOAL", "Related Mục tiêu").strip()
PROP_TYPE = os.getenv("PROP_TYPE", "Loại công việc")
PROP_PRIORITY = os.getenv("PROP_PRIORITY", "Cấp độ")
PROP_NOTE = os.getenv("PROP_NOTE", "note")

# Goals DB properties
GOAL_PROP_STATUS = "Trạng thái"
GOAL_PROP_START = "Ngày bắt đầu"
GOAL_PROP_END = "Ngày hoàn thành"
GOAL_PROP_COUNTDOWN = "Đếm ngược"
GOAL_PROP_PROGRESS = "Tiến độ"
GOAL_PROP_TOTAL_TASKS = "Tổng nhiệm vụ cần làm"
GOAL_PROP_DONE_TASKS = "Nhiệm vụ đã hoàn thành"
GOAL_PROP_REMAIN = "Nhiệm vụ còn lại"
GOAL_PROP_DONE_WEEK = "Nhiệm vụ hoàn thành tuần này"
GOAL_PROP_DONE_MONTH = "Nhiệm vụ hoàn thành tháng này"

# Lưu LAST_TASKS theo chat_id để tránh lệch trạng thái
LAST_TASKS = {}  # {chat_id: [page_id, ...]}

print(f"[CONFIG] GOALS_DB={GOALS_DB[:8]}... REMIND_DB={REMIND_DB[:8]}...")

# ============================================================================
# NOTION HELPERS
# ============================================================================
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
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    payload = {"page_size": page_size}
    if filter_payload:
        if isinstance(filter_payload, dict):
            if "and" in filter_payload or "filter" in filter_payload or "or" in filter_payload:
                payload.update({"filter": filter_payload} if "filter" not in filter_payload else filter_payload)
            else:
                payload["filter"] = filter_payload
    try:
        r = requests.post(url, headers=HEADERS, json=payload, timeout=20)
        if r.status_code != 200:
            print(f"[ERROR] Notion query {r.status_code}: {r.text[:500]}")
            return []
        return r.json().get("results", [])
    except Exception as e:
        print(f"[ERROR] notion_query: {e}")
        return []

def notion_create_page(db_id, properties):
    try:
        return req_post("/pages", {"parent": {"database_id": db_id}, "properties": properties})
    except Exception as e:
        print(f"[ERROR] create page: {e}")
        return None

def notion_update_page(page_id, properties):
    try:
        return req_patch(f"/pages/{page_id}", {"properties": properties})
    except Exception as e:
        print(f"[ERROR] update page: {e}")
        return None

# ============================================================================
# UTILITY HELPERS
# ============================================================================
def format_dt(dt_obj):
    if not dt_obj:
        return ""
    if isinstance(dt_obj, datetime.date) and not isinstance(dt_obj, datetime.datetime):
        return dt_obj.strftime("%d/%m/%Y")
    try:
        if dt_obj.tzinfo is None:
            dt = TZ.localize(dt_obj)
        else:
            dt = dt_obj.astimezone(TZ)
        return dt.strftime("%d/%m/%Y %H:%M")
    except:
        return str(dt_obj)

def get_title(page):
    p = page.get("properties", {}).get(PROP_TITLE)
    if p and p.get("type") == "title":
        return "".join([t.get("plain_text", "") for t in p.get("title", [])])
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
        print(f"[TELEGRAM DISABLED]\n{text}\n")
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
        return response.status_code == 200
    except Exception as e:
        print(f"[ERROR] Telegram: {e}")
        return False

def send_telegram_long(text):
    max_len = 3800
    parts = [text[i:i+max_len] for i in range(0, len(text), max_len)]
    for part in parts:
        send_telegram(part)
        time.sleep(0.5)

def render_progress_bar(percent, length=18):
    try:
        pct = int(round(float(percent)))
    except:
        pct = 0
    pct = max(0, min(100, pct))
    filled_len = int(round(length * pct / 100))
    bar = "█" * filled_len + "░" * (length - filled_len)
    return f"[{bar}] {pct}%"

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

# ============================================================================
# GOAL HELPERS
# ============================================================================
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
    """Đọc properties của goal page"""
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
    
    title = ""
    for k, v in props.items():
        if v.get("type") == "title":
            title = extract_plain_text_from_rich_text(v.get("title", []))
            break
    out["title"] = title or get_title(goal_page) or out["id"]

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

    if out.get("dem_nguoc_formula") is None and out.get("ngay_hoan_thanh"):
        try:
            today = datetime.datetime.now(TZ).date()
            out["days_remaining_computed"] = (out["ngay_hoan_thanh"] - today).days
        except:
            out["days_remaining_computed"] = None

    # Normalize progress
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
        except:
            progress_pct = None

    if progress_pct is None:
        try:
            total = out.get("tong_nhiem_vu_rollup")
            done = out.get("nhiem_vu_da_hoan_rollup")
            if total and (done is not None):
                progress_pct = int(round(float(done) / float(total) * 100)) if total > 0 else 0
        except:
            progress_pct = None

    out["progress_pct"] = progress_pct
    return out

def _parse_completed_datetime_from_page(page):
    dt = get_date_start(page, PROP_COMPLETED)
    if dt:
        return dt
    try:
        props = page.get("properties", {}) or {}
        s = extract_prop_text(props, PROP_COMPLETED)
        if s:
            return dateparser.parse(s)
    except:
        pass
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

# ============================================================================
# AI CORE ENGINE - PHÂN TÍCH SÂU & LẬP KẾ HOẠCH
# ============================================================================

@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
def call_gpt(messages, model="gpt-4o-mini", temperature=0.7, max_tokens=2000):
    """Gọi GPT với retry logic"""
    if not OPENAI_API_KEY:
        return "AI không khả dụng - thiếu OPENAI_API_KEY"
    
    openai.api_key = OPENAI_API_KEY
    try:
        resp = openai.ChatCompletion.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[ERROR] call_gpt: {e}")
        raise

def ai_deep_weekly_analysis(context):
    """
    AI PHÂN TÍCH CHIẾN LƯỢC TUẦN
    - Đánh giá thực trạng
    - Phát hiện patterns & bottlenecks  
    - Đưa ra chiến lược tuần tới
    - Message động lực cá nhân hóa
    """
    
    prompt = f"""Bạn là chiến lược gia cá nhân, kết hợp tư duy analytics và empathy sâu sắc.

📊 TÌNH HÌNH TUẦN VỪA QUA:
• Tổng công việc: {context['total_tasks']}
• Hoàn thành đúng hạn: {context['completed_ontime']}
• Hoàn thành trễ hạn: {context['completed_overdue']}
• Quá hạn chưa làm: {context['overdue_unfinished']}
• Tỷ lệ hoàn thành: {context['completion_rate']:.1f}%

🎯 MỤC TIÊU CHÍNH:
• "{context['goal_title']}"
• Tiến độ: {context['goal_progress']:.1f}% ({context['goal_done']}/{context['goal_total']})
• Tốc độ tuần này: {context['goal_velocity']} tasks
• Tốc độ cần thiết: {context['required_velocity']:.1f} tasks/tuần

📈 PHÂN BỔ CÔNG VIỆC:
{context['workload_distribution']}

⚠️ VẤN ĐỀ PHÁT HIỆN:
{context['detected_issues']}

---

HÃY PHẢN HỒI THEO FORMAT:

🔍 NHẬN ĐỊNH:
[2-3 câu phân tích thực tế, thẳng thắn về tình hình]

⚡ INSIGHT QUAN TRỌNG:
[1-2 phát hiện sâu về pattern làm việc, điểm nghẽn]

🎯 CHIẾN LƯỢC TUẦN TỚI:
[3-4 actions cụ thể, có thể thực hiện. Mỗi action 1 dòng với emoji]

💪 LỜI ĐỘNG VIÊN:
[2-3 câu động lực chân thực, tạo năng lượng bắt tay vào làm]

Yêu cầu:
- Giọng điệu: người anh/chị đi trước
- Thẳng thắn nhưng động viên
- Cụ thể, có thể hành động
- Không dùng từ ngữ sáo, generic
"""

    try:
        return call_gpt([
            {"role": "system", "content": "You are a strategic life coach who combines data analysis with deep human understanding. Speak Vietnamese naturally."},
            {"role": "user", "content": prompt}
        ], temperature=0.8, max_tokens=1500)
    except Exception as e:
        print(f"[ERROR] ai_deep_weekly_analysis: {e}")
        return _fallback_analysis(context)

def ai_smart_planning(next_week_tasks, goal_info, analysis_context):
    """
    AI LẬP KẾ HOẠCH TUẦN TỚI
    - Phân bổ tasks theo năng lực thực tế
    - Time blocking 7 ngày
    - Đề xuất priorities động
    - Milestones & rủi ro
    """
    
    tasks_summary = []
    for t in next_week_tasks[:20]:
        title = get_title(t)
        due = get_date_start(t, PROP_DUE)
        priority = get_select_name(t, PROP_PRIORITY)
        
        tasks_summary.append({
            "title": title[:50],
            "due": str(due.date()) if due else "No deadline",
            "priority": priority or "Medium"
        })
    
    prompt = f"""Bạn là AI planner chuyên nghiệp, thiết kế kế hoạch thực tế và khả thi.

📋 CÔNG VIỆC TUẦN TỚI ({len(next_week_tasks)} tasks):
{json.dumps(tasks_summary, ensure_ascii=False, indent=2)}

🎯 MỤC TIÊU:
• {goal_info['title']}
• Còn lại: {goal_info['total_tasks'] - goal_info['done_tasks']} tasks
• Tốc độ cần: {analysis_context['required_velocity']:.1f} tasks/tuần

💡 NĂNG LỰC THỰC TẾ:
• Tỷ lệ hoàn thành tuần trước: {analysis_context['completion_rate']:.1f}%
• Vấn đề: {analysis_context['detected_issues']}

---

TẠO KẾ HOẠCH THEO FORMAT:

📅 KẾ HOẠCH TUẦN:

**Thứ 2 - KHỞI ĐỘNG**
[2-3 tasks ưu tiên cao nhưng không quá nặng]

**Thứ 3-4 - PEAK PERFORMANCE**
[Tasks khó nhất khi năng lượng cao]

**Thứ 5 - BUFFER DAY**
[Tasks trung bình, để không gian xử lý phát sinh]

**Thứ 6 - TỐC ĐỘ**
[Hoàn thiện tasks nhỏ]

**Thứ 7-CN - REVIEW**
[Review tuần + chuẩn bị tuần sau]

🎯 3 MỐC QUAN TRỌNG:
[3 milestones phải đạt trong tuần]

⚠️ RỦI RO CẦN TRÁNH:
[2-3 điểm có thể trật bánh + cách phòng tránh]

Yêu cầu:
- Thực tế với năng lực hiện tại
- Tạo momentum tăng dần
- Buffer cho phát sinh
"""

    try:
        return call_gpt([
            {"role": "system", "content": "You are an expert weekly planner who creates realistic schedules. Answer in Vietnamese."},
            {"role": "user", "content": prompt}
        ], temperature=0.7, max_tokens=2000)
    except Exception as e:
        print(f"[ERROR] ai_smart_planning: {e}")
        return "Kế hoạch chi tiết sẽ được tạo sau."

def _fallback_analysis(context):
    """Fallback khi AI không available"""
    if context['completion_rate'] >= 70:
        return """🔍 NHẬN ĐỊNH:
Tuần này bạn làm việc hiệu quả với tỷ lệ hoàn thành tốt.

⚡ INSIGHT:
Hãy duy trì momentum và tăng tốc ở tasks quan trọng.

🎯 CHIẾN LƯỢC:
• Tập trung hoàn thiện mục tiêu chính
• Xử lý việc quá hạn tồn đọng  
• Review và lập kế hoạch

💪 ĐỘNG VIÊN:
Bạn đang trên đà tốt. Tiếp tục như vậy!"""
    else:
        return """🔍 NHẬN ĐỊNH:
Tuần này có việc chưa hoàn thành như kế hoạch.

⚡ INSIGHT:
Xem lại workload và ưu tiên việc thực sự quan trọng.

🎯 CHIẾN LƯỢC:
• Giảm số lượng tasks, tăng chất lượng
• Focus 3-5 việc quan trọng nhất
• Tạo buffer cho phát sinh

💪 ĐỘNG VIÊN:
Bắt đầu lại với những bước nhỏ, chắc chắn."""

# ============================================================================
# JOB WEEKLY - PHIÊN BẢN NÂNG CẤP VỚI AI CAN THIỆP SÂU
# ============================================================================

def job_weekly():
    """
    BÁO CÁO TUẦN với AI CAN THIỆP SÂU:
    1. Thu thập dữ liệu thực tế
    2. Phát hiện vấn đề & patterns
    3. AI phân tích chiến lược
    4. AI lập kế hoạch tuần tới
    5. Gửi báo cáo đầy đủ
    """
    print(f"\n{'='*60}")
    print(f"[WEEKLY] Started at {datetime.datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    today = datetime.datetime.now(TZ).date()
    week_start = today - datetime.timedelta(days=today.weekday())
    week_end = week_start + datetime.timedelta(days=6)

    # ========================================================================
    # BƯỚC 1: THU THẬP DỮ LIỆU TUẦN VỪA QUA
    # ========================================================================
    print("[1/6] Thu thập dữ liệu từ Notion...")
    
    all_tasks = notion_query(
        REMIND_DB,
        {
            "and": [
                {"property": PROP_DONE, "checkbox": {"equals": False}},
                {"or": [
                    {"property": PROP_DUE, "date": {
                        "on_or_after": week_start.isoformat(),
                        "on_or_before": week_end.isoformat()
                    }},
                    {"property": PROP_DUE, "date": {
                        "before": week_start.isoformat()
                    }}
                ]}
            ]
        }
    ) or []


    # Phân tích tasks
    completed_ontime = 0
    completed_overdue = 0
    overdue_unfinished = 0
    workload_by_day = defaultdict(int)
    priority_dist = defaultdict(int)

    for t in all_tasks:
        is_done = get_checkbox(t, PROP_DONE)
        due_date = get_date_start(t, PROP_DUE)
        priority = get_select_name(t, PROP_PRIORITY)
        
        if priority:
            priority_dist[priority] += 1
        
        if due_date:
            workload_by_day[due_date.date().strftime("%a")] += 1
            
            if is_done:
                completed_date = _parse_completed_datetime_from_page(t)
                if completed_date:
                    comp_d = completed_date.date() if isinstance(completed_date, datetime.datetime) else completed_date
                    if comp_d <= due_date.date():
                        completed_ontime += 1
                    else:
                        completed_overdue += 1
                else:
                    completed_ontime += 1  # Assume on time if no completed date
            else:
                if due_date.date() < today:
                    overdue_unfinished += 1

    total_tasks = len(all_tasks)
    completed_total = completed_ontime + completed_overdue
    completion_rate = (completed_total / total_tasks * 100) if total_tasks > 0 else 0

    # ========================================================================
    # BƯỚC 2: PHÂN TÍCH MỤC TIÊU
    # ========================================================================
    print("[2/6] Phân tích mục tiêu...")
    
    goals = notion_query(GOALS_DB) or []
    top_goal = None
    
def pick_top_goal(goals):
    """
    Ưu tiên:
    1. Status = In progress
    2. Có ngày hoàn thành gần nhất
    3. Progress < 100
    """
    candidates = []

    today = datetime.datetime.now(TZ).date()

    for g in goals:
        ginfo = read_goal_properties(g)
        if ginfo.get("progress_pct") is None:
            continue
        if ginfo.get("progress_pct") >= 100:
            continue

        end_date = ginfo.get("ngay_hoan_thanh")
        days_left = (
            (end_date - today).days
            if end_date else 9999
        )

        candidates.append((days_left, ginfo))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]

    
    if not top_goal:
        top_goal = pick_top_goal(goals)

    progress_pct = int(top_goal.get("progress_pct", 0))
    done_tasks_week = top_goal.get("nhiem_vu_hoan_tuan_rollup", 0)
    total_tasks_goal = top_goal.get("tong_nhiem_vu_rollup", 0)
    done_tasks_goal = top_goal.get("nhiem_vu_da_hoan_rollup", 0)
    
    # Tính velocity cần thiết
    
    weeks_remaining = 1  # default an toàn

    if top_goal.get("ngay_hoan_thanh"):
        days_left = (top_goal["ngay_hoan_thanh"] - today).days
        weeks_remaining = max(1, ceil(days_left / 7))

    tasks_remaining = max(0, total_tasks_goal - done_tasks_goal)
    required_velocity = round(tasks_remaining / weeks_remaining, 2)


    # ========================================================================
    # BƯỚC 3: PHÁT HIỆN VẤN ĐỀ & XÁC ĐỊNH TÌNH TRẠNG
    # ========================================================================
    print("[3/6] Phát hiện vấn đề và patterns...")
    
    detected_issues = []
    if overdue_unfinished >= 3:
        detected_issues.append("⚠️ Nhiều việc quá hạn chưa xử lý → Quá tải hoặc ưu tiên chưa đúng")
    if completed_overdue >= 3:
        detected_issues.append("⏰ Hoàn thành nhiều việc trễ → Deadline estimation cần cải thiện")
    if done_tasks_week == 0:
        detected_issues.append("🎯 Chưa đóng góp vào mục tiêu chính → Mất focus")
    if completion_rate < 50:
        detected_issues.append("📉 Tỷ lệ hoàn thành thấp → Cần giảm workload hoặc tăng discipline")
    
    if not detected_issues:
        detected_issues.append("✅ Không phát hiện vấn đề nghiêm trọng")

    # ========================================================================
    # BƯỚC 4: AI PHÂN TÍCH CHIẾN LƯỢC
    # ========================================================================
    print("[4/6] AI đang phân tích chiến lược...")
    
    workload_dist = "\n".join([f"  • {day}: {count} tasks" for day, count in sorted(workload_by_day.items())])
    
    analysis_context = {
        'total_tasks': total_tasks,
        'completed_ontime': completed_ontime,
        'completed_overdue': completed_overdue,
        'overdue_unfinished': overdue_unfinished,
        'completion_rate': completion_rate,
        'goal_title': top_goal['title'],
        'goal_progress': progress_pct,
        'goal_done': done_tasks_goal,
        'goal_total': total_tasks_goal,
        'goal_velocity': done_tasks_week,
        'required_velocity': required_velocity,
        'workload_distribution': workload_dist or "  • Không có dữ liệu",
        'detected_issues': "\n".join(detected_issues)
    }

    if ENABLE_AI:
        try:
            ai_analysis = ai_deep_weekly_analysis(analysis_context)
        except Exception as e:
            print("[WARN] AI weekly analysis failed:", e)
            ai_analysis = _fallback_analysis(analysis_context)
    else:
        ai_analysis = _fallback_analysis(analysis_context)


    # ========================================================================
    # BƯỚC 5: AI LẬP KẾ HOẠCH TUẦN TỚI
    # ========================================================================
    print("[5/6] AI đang lập kế hoạch tuần tới...")
    
    next_week_start = week_end + datetime.timedelta(days=1)
    next_week_end = next_week_start + datetime.timedelta(days=6)
    
    next_week_tasks = notion_query(
        REMIND_DB,
        {
            "and": [
                {"property": PROP_DUE, "date": {"on_or_after": next_week_start.isoformat()}},
                {"property": PROP_DUE, "date": {"on_or_before": next_week_end.isoformat()}},
                {"property": PROP_DONE, "checkbox": {"equals": False}}
            ]
        }
    ) or []

    if ENABLE_AI:
        try:
            ai_plan = ai_smart_planning(next_week_tasks, top_goal, analysis_context)
        except Exception as e:
            print("[WARN] AI weekly planning failed:", e)
            ai_plan = "⚠️ Kế hoạch chi tiết sẽ được tạo sau khi hệ thống ổn định."
    else:
        ai_plan = "ℹ️ AI hiện đang tắt. Kế hoạch tuần sẽ được tạo thủ công."


    # ========================================================================
    # BƯỚC 6: BUILD BÁO CÁO HOÀN CHỈNH
    # ========================================================================
    print("[6/6] Tạo báo cáo và gửi...")
    
    progress_bar = "█" * int(progress_pct / 10) + "░" * (10 - int(progress_pct / 10))

    message = f"""
📊 <b>BÁO CÁO TUẦN — {week_start.strftime('%d/%m')} đến {week_end.strftime('%d/%m/%Y')}</b>

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
<b>📈 TỔNG QUAN TUẦN VỪA QUA</b>

<b>Công việc hàng ngày:</b>
  ✅ Hoàn thành đúng hạn: <b>{completed_ontime}</b>
  ⏰ Hoàn thành trễ: {completed_overdue}
  🆘 Quá hạn chưa làm: {overdue_unfinished}
  📊 Tỷ lệ hoàn thành: <b>{completion_rate:.1f}%</b>

<b>Mục tiêu chính:</b>
  🎯 {top_goal['title']}
  📈 Tiến độ: <b>{progress_pct}%</b> [{progress_bar}]
  ⚡ Tốc độ tuần này: {done_tasks_week} tasks
  🎪 Cần duy trì: {required_velocity:.1f} tasks/tuần

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
<b>🤖 PHÂN TÍCH & CHIẾN LƯỢC</b>

{ai_analysis}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
<b>📅 KẾ HOẠCH TUẦN TỚI</b>

{ai_plan}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
<i>Generated by AI • {datetime.datetime.now(TZ).strftime('%H:%M %d/%m/%Y')}</i>
"""

    send_telegram_long(message.strip())
    
    print(f"\n✅ Báo cáo tuần đã gửi!")
    print(f"{'='*60}\n")

# ============================================================================
# JOB DAILY - GIỮ NGUYÊN CODE CŨ
# ============================================================================

def job_daily():
    now = datetime.datetime.now(TZ)
    today = datetime.datetime.now(TZ).date()

    print("[INFO] job_daily start, today =", today.isoformat())

    filters = [
        {"property": PROP_DONE, "checkbox": {"equals": False}},
        {"property": PROP_DUE, "date": {"is_not_empty": True}}
    ]
    if PROP_ACTIVE:
        filters.insert(0, {"property": PROP_ACTIVE, "checkbox": {"equals": True}})

    try:
        all_tasks = notion_query(REMIND_DB, {"and": filters}) or []
        print(f"[DBG] fetched {len(all_tasks)} active tasks")
    except Exception as e:
        print("[WARN] job_daily failed:", e)
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

            # Priority rule
            if pri == "cao" and days_left <= 2:
                tasks.append(p)
            elif pri in ("tb", "trung bình") and days_left <= 1:
                tasks.append(p)
            elif pri == "thấp" and days_left <= 0:
                tasks.append(p)

        except Exception as e:
            print("[WARN] skipping task:", e)
            continue

    print(f"[DBG] daily reminder tasks: {len(tasks)}")

    lines = [
        f"📋 <b>Hôm nay {today.strftime('%d/%m/%Y')} sắp có {len(tasks)} nhiệm vụ hằng ngày</b>",
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

            d = overdue_days(p)
            if d is None:
                sys_note = ""
            elif d > 0:
                sys_note = f"↳⏰ Đã trễ {d} ngày, làm ngay đi sếp ơi!"
            elif d == 0:
                sys_note = "↳💥Làm Ngay Hôm nay!"
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

    # Goals section
    goal_map = {}
    for p in tasks:
        rels = p.get("properties", {}).get(PROP_REL_GOAL, {}).get("relation", [])
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
                header += f"còn {drem} ngày" if drem > 0 else "hết hạn hôm nay" if drem == 0 else f"đã trễ {-drem} ngày"
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
                    sys_note = "↳💥Làm Ngay Hôm nay!"
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
        lines.append(f"🎯 sắp có {total_goal_tasks_due} nhiệm vụ Mục tiêu")
        lines.extend(goal_lines)

    send_telegram("\n".join(lines).strip())

    global LAST_TASKS
    LAST_TASKS = [p.get("id") for p in tasks if p and isinstance(p, dict)]

# ============================================================================
# JOB MONTHLY - GIỮ NGUYÊN CODE CŨ
# ============================================================================

def job_monthly():
    today = datetime.datetime.now(TZ).date()
    mstart, mend = month_range(today)
    print(f"[INFO] job_monthly start for {mstart} -> {mend}")

    filters_done = [{"property": PROP_DONE, "checkbox": {"equals": True}}]
    if PROP_ACTIVE:
        filters_done.insert(0, {"property": PROP_ACTIVE, "checkbox": {"equals": True}})

    try:
        done_pages = notion_query(REMIND_DB, {"and": filters_done})
        print(f"[DBG] job_monthly: fetched {len(done_pages)} done pages")
    except Exception as e:
        print("[WARN] job_monthly failed:", e)
        done_pages = []

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
            print("[WARN] error parsing completed date:", ex)
            continue

    daily_month_done = 0
    for p, comp_date in done_this_month:
        try:
            ttype = get_select_name(p, PROP_TYPE) or ""
            if "hằng" in ttype.lower():
                daily_month_done += 1
        except:
            continue

    overdue_done = 0
    for p, comp_date in done_this_month:
        try:
            due = get_date_start(p, PROP_DUE)
            if due and comp_date and comp_date > due.date():
                overdue_done += 1
        except:
            continue

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
        print("[WARN] overdue query failed:", e)
        overdue_remaining = 0

    goals_summary = []
    if GOALS_DB:
        try:
            goals = notion_query(GOALS_DB)
            print(f"[DBG] fetched {len(goals)} goals")
        except Exception as e:
            print("[WARN] goals query failed:", e)
            goals = []
        
        for g in goals:
            try:
                ginfo = read_goal_properties(g)
            except Exception as e:
                print("[WARN] read_goal_properties failed:", e)
                ginfo = {}

            total = ginfo.get("tong_nhiem_vu_rollup")
            done_total = ginfo.get("nhiem_vu_da_hoan_rollup")
            monthly_done = ginfo.get("nhiem_vu_hoan_thang_rollup") or 0
            progress_pct = ginfo.get("progress_pct") or 0

            gs = {
                "name": ginfo.get("title") or "(no title)",
                "progress": int(progress_pct),
                "done": done_total or 0,
                "total": total or 0,
                "monthly_done": monthly_done
            }
            goals_summary.append(gs)

    lines = [f"📅 <b>Báo cáo tháng {today.strftime('%m/%Y')}</b>", ""]
    lines.append(f"• ✔ Việc hằng ngày hoàn thành tháng: {daily_month_done}")
    lines.append(f"• ⏳ Quá hạn đã hoàn thành: {overdue_done}")
    lines.append(f"• 🆘 Quá hạn chưa làm: {overdue_remaining}")
    lines.append("")
    lines.append("🎯 Tiến độ mục tiêu chính:")
    
    for g in sorted(goals_summary, key=lambda x: -x['progress'])[:8]:
        bar = render_progress_bar(g['progress'])
        lines.append(f"• {g['name']} → {g['progress']}% ({g['done']}/{g['total']}) {bar}")
        lines.append(f"  → Nhiệm vụ hoàn thành tháng này: {g['monthly_done']}")
    
    lines.append("")
    lines.append("📈 <b>Tổng quan</b>")
    lines.append("Sếp đang tiến rất tốt! Hãy lăn quả tuyết này để tiến tới hoàn thành mục tiêu lớn. 🎯 Tháng sau bứt phá thêm nhé! 🔥🔥🔥")

    send_telegram("\n".join(lines).strip())
    print(f"[INFO] job_monthly sent")

# ============================================================================
# TELEGRAM WEBHOOK HANDLERS - GIỮ NGUYÊN
# ============================================================================

@app.route("/webhook", methods=["POST"])
def webhook():
    global LAST_TASKS
    try:
        update = request.get_json(silent=True) or {}
        message = update.get("message", {}) or {}
        chat_id = str(message.get("chat", {}).get("id", ""))
        if TELEGRAM_CHAT_ID and chat_id != TELEGRAM_CHAT_ID:
            return jsonify({"ok": False, "error": "forbidden"}), 403
        text = (message.get("text", "") or "").strip()
        if not text.startswith("/"):
            return jsonify({"ok": True}), 200
        
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

            tasks = notion_query(REMIND_DB, {"and": filters}) or []

            if not tasks:
                send_telegram("🎉 Không có nhiệm vụ trong tuần này hoặc quá hạn.")
                return jsonify({"ok": True}), 200

            lines = [f"📋 <b>Danh sách nhiệm vụ tuần {start_week.strftime('%d/%m')} - {end_week.strftime('%d/%m')}</b>", ""]

            visible_tasks = []
            for p in tasks:
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
                    
                    d = overdue_days(p)
                    if d is None:
                        sys_note = ""
                    elif d > 0:
                        sys_note = f"↳⏰ Đã trễ {d} ngày"
                    elif d == 0:
                        sys_note = "↳💥 Làm ngay hôm nay!"
                    else:
                        sys_note = f"↳⏳ Còn {abs(d)} ngày nữa"

                    line = f"{len(visible_tasks)+1} {sym} <b>{title}</b> — Cấp độ: {pri}{due_text}"

                    if note_text:
                        line += f"\n📝 {note_text}"
                    if sys_note:
                        line += f"\n  {sys_note}"

                    lines.append(line)
                    visible_tasks.append(p)

                except Exception as e:
                    print("[ERROR] formatting /check task:", e)
                    continue

            LAST_TASKS[chat_id] = [
                p.get("id") for p in visible_tasks if p and isinstance(p, dict)
            ]

            send_telegram("\n".join(lines))
            return jsonify({"ok": True}), 200

        elif text.lower().startswith("/done."):
            parts = text.split(".", 1)

            if len(parts) < 2 or not parts[1].strip().isdigit():
                send_telegram("❌ Số không hợp lệ. Gõ /done.<số> (ví dụ /done.1).")
                return jsonify({"ok": True}), 200

            n = int(parts[1].strip())

            task_list = LAST_TASKS.get(chat_id, [])

            if n < 1 or n > len(task_list):
                send_telegram("❌ Số không hợp lệ. Gõ /check để xem danh sách.")
                return jsonify({"ok": True}), 200

            page_id = task_list[n - 1]

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
            except:
                send_telegram("❌ Không parse được ngày/giờ.")
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
                send_telegram(f"✅ Đã tạo: {name} — hạn: {dt.strftime('%d/%m/%Y %H:%M')} — {priority}")
            else:
                send_telegram("❌ Lỗi tạo nhiệm vụ.")
            return jsonify({"ok": True}), 200

        send_telegram("❓ Lệnh không nhận diện. Dùng /check, /done.<n>, /new")
        return jsonify({"ok": True}), 200
    except Exception as e:
        print("Unhandled webhook error:", e)
        send_telegram("❌ Lỗi nội bộ.")
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

MANUAL_TRIGGER_SECRET = os.getenv("MANUAL_TRIGGER_SECRET", "").strip()

@app.route("/debug/run_weekly", methods=["POST", "GET"])
def debug_run_weekly():
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

# ============================================================================
# SCHEDULER
# ============================================================================

def start_scheduler():
    sched = BackgroundScheduler(timezone=TIMEZONE)

    try:
        if 'job_daily' in globals():
            sched.add_job(job_daily, 'cron', hour=REMIND_HOUR, minute=REMIND_MINUTE, id='daily')
    except Exception as e:
        print("[ERROR] adding daily job:", e)

    try:
        if 'job_weekly' in globals():
            sched.add_job(job_weekly, 'cron', day_of_week='sun', hour=WEEKLY_HOUR, minute=0, id='weekly')
    except Exception as e:
        print("[ERROR] adding weekly job:", e)

    def monthly_wrapper():
        today = datetime.datetime.now(TZ).date()
        tomorrow = today + datetime.timedelta(days=1)
        if tomorrow.day == 1:
            try:
                if 'job_monthly' in globals():
                    job_monthly()
            except Exception as e:
                print("[ERROR] monthly job:", e)

    try:
        sched.add_job(monthly_wrapper, 'cron', hour=MONTHLY_HOUR, minute=0, id='monthly')
    except Exception as e:
        print("[ERROR] adding monthly wrapper:", e)

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
            print("setWebhook response:", r.status_code, r.json() if r.status_code == 200 else r.text)
        except Exception as e:
            print("Error setting webhook:", e)

# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    if not NOTION_TOKEN or not REMIND_DB:
        print("FATAL: NOTION_TOKEN or REMIND_NOTION_DATABASE not set. Exiting.")
        raise SystemExit(1)

    if "Authorization" not in HEADERS and NOTION_TOKEN:
        HEADERS["Authorization"] = f"Bearer {NOTION_TOKEN}"

    print("Notion configured:", bool(NOTION_TOKEN), REMIND_DB[:8] + "...")
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        print("Telegram configured: chat_id present.")
    else:
        print("Telegram NOT configured. Messages will print to console.")

    if TELEGRAM_TOKEN and WEBHOOK_URL:
        set_telegram_webhook()

    start_scheduler()

    if RUN_ON_START:
        try:
            print("RUN_ON_START -> running job_daily() once.")
            job_daily()
        except Exception as e:
            print("Error running job_daily on start:", e)

    BACKGROUND_WORKER = os.getenv("BACKGROUND_WORKER", "true").lower() in ("1", "true", "yes")
    if BACKGROUND_WORKER:
        print("Running in BACKGROUND_WORKER mode. Process will stay alive.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("Shutting down.")
    else:
        port = int(os.getenv("PORT", 5000))
        print(f"Starting Flask server on port {port}.")
        app.run(host="0.0.0.0", port=port, threaded=True)
