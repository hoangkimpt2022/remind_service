#!/usr/bin/env python3
# remind_service_full.py - FIXED VERSION với AI luôn chạy
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
from openai import OpenAI
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
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
client = OpenAI(api_key=OPENAI_API_KEY)

TIMEZONE = os.getenv("TIMEZONE", "Asia/Ho_Chi_Minh")
TZ = pytz.timezone(TIMEZONE)

REMIND_HOUR = int(os.getenv("REMIND_HOUR", "14"))
REMIND_MINUTE = int(os.getenv("REMIND_MINUTE", "0"))
WEEKLY_HOUR = int(os.getenv("WEEKLY_HOUR", "20"))
MONTHLY_HOUR = int(os.getenv("MONTHLY_HOUR", "8"))
RUN_ON_START = os.getenv("RUN_ON_START", "false").lower() in ("1", "true", "yes")

HEADERS = {
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
    "Authorization": f"Bearer {NOTION_TOKEN}"
}

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

LAST_TASKS = {}

print(f"[CONFIG] OpenAI API: {'✓' if OPENAI_API_KEY else '✗'}")
print(f"[CONFIG] GOALS_DB: {GOALS_DB[:8] if GOALS_DB else 'NOT SET'}...")
print(f"[CONFIG] REMIND_DB: {REMIND_DB[:8] if REMIND_DB else 'NOT SET'}...")

# ============================================================================
# NOTION HELPERS
# ============================================================================
def notion_query(db_id, filter_payload=None, page_size=100):
    if not db_id:
        return []
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    payload = {"page_size": page_size}
    if filter_payload:
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

def notion_update_page(page_id, properties):
    try:
        url = f"https://api.notion.com/v1/pages/{page_id}"
        r = requests.patch(url, headers=HEADERS, json={"properties": properties}, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[ERROR] update page: {e}")
        return None

def notion_create_page(db_id, properties):
    try:
        url = "https://api.notion.com/v1/pages"
        r = requests.post(url, headers=HEADERS, json={"parent": {"database_id": db_id}, "properties": properties}, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[ERROR] create page: {e}")
        return None

# ============================================================================
# UTILITY HELPERS
# ============================================================================
def get_title(page):
    for v in page.get("properties", {}).values():
        if v.get("type") == "title" and v.get("title"):
            return "".join([t.get("plain_text", "") for t in v.get("title", [])])
    return "Untitled"

def get_checkbox(page, prop_name):
    return bool(page.get("properties", {}).get(prop_name, {}).get("checkbox", False))

def get_select_name(page, prop_name):
    sel = page.get("properties", {}).get(prop_name, {}).get("select")
    return sel.get("name", "") if sel else ""

def get_date_start(page, prop_name):
    raw = page.get("properties", {}).get(prop_name, {}).get("date", {}).get("start")
    if raw:
        try:
            return dateparser.parse(raw)
        except:
            pass
    return None

def overdue_days(page):
    due_dt = get_date_start(page, PROP_DUE)
    if not due_dt:
        return None
    today = datetime.datetime.now(TZ).date()
    return (today - due_dt.date()).days

def render_progress_bar(percent, total_blocks=10):
    """
    Render progress bar dạng text.
    Ví dụ: 60% → ██████░░░░
    """
    try:
        percent = max(0, min(100, int(percent)))
        filled = round(percent / 100 * total_blocks)
        return "█" * filled + "░" * (total_blocks - filled)
    except Exception:
        return "░" * total_blocks

def _parse_completed_datetime_from_page(page):
    """
    Lấy ngày hoàn thành (Completed date) từ Notion page.
    Trả về datetime/date hoặc None nếu không có.
    """
    try:
        raw = (
            page
            .get("properties", {})
            .get(PROP_COMPLETED, {})
            .get("date", {})
            .get("start")
        )
        if not raw:
            return None

        # Dùng dateutil nếu có, fallback sang datetime
        try:
            return dateparser.parse(raw)
        except Exception:
            return datetime.datetime.fromisoformat(raw)

    except Exception:
        return None

def week_range(date_obj):
    start = date_obj - datetime.timedelta(days=date_obj.weekday())
    end = start + datetime.timedelta(days=6)
    return start, end

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[TELEGRAM DISABLED]\n{text}\n")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        response = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=10)
        return response.status_code == 200
    except Exception as e:
        print(f"[ERROR] Telegram: {e}")
        return False

def send_telegram_long(text):
    max_len = 3800
    for i in range(0, len(text), max_len):
        send_telegram(text[i:i+max_len])
        time.sleep(0.5)

def priority_emoji(priority: str) -> str:
    p = (priority or "").strip().lower()
    if p == "cao": return "🔴"
    if p in ("tb", "trung bình"): return "🟡"
    if p == "thấp": return "🟢"
    return "🟡"

# ============================================================================
# GOAL HELPERS
# ============================================================================
def extract_plain_text(rich):
    if not rich: return ""
    return "".join(part.get("plain_text","") for part in rich)

def find_prop_key(props, key_like):
    if key_like in props: return key_like
    low = key_like.lower()
    for k in props.keys():
        if k.lower() == low or low in k.lower():
            return k
    return None

def safe_formula(props, name):
    k = find_prop_key(props, name)
    if not k: return None
    f = props.get(k, {}).get("formula", {})
    if f.get("string") is not None: return f["string"]
    if f.get("number") is not None: return f["number"]
    return None

def get_note_text(page):
    """
    Lấy nội dung note (rich_text) từ Notion page
    Trả về string, an toàn không crash
    """
    try:
        prop = page.get("properties", {}).get(PROP_NOTE, {})
        if prop.get("type") == "rich_text":
            texts = prop.get("rich_text", [])
            return "".join(t.get("plain_text", "") for t in texts).strip()

        # fallback nếu Notion trả structure khác
        arr = prop.get("rich_text") or prop.get("title") or []
        return "".join(t.get("plain_text", "") for t in arr).strip()
    except Exception:
        return ""
        
def safe_rollup(props, name):
    k = find_prop_key(props, name)
    if not k: return None
    ru = props.get(k, {}).get("rollup", {})
    if ru.get("number") is not None: return ru["number"]
    arr = ru.get("array", [])
    return len(arr) if isinstance(arr, list) else None

def safe_date(props, name):
    k = find_prop_key(props, name)
    if not k: return None
    raw = props.get(k, {}).get("date", {}).get("start")
    if raw:
        try:
            return dateparser.parse(raw).date()
        except:
            pass
    return None

def read_goal_properties(goal_page):
    out = {"id": goal_page.get("id", ""), "title": "Untitled"}
    props = goal_page.get("properties", {})
    
    # Get title
    for v in props.values():
        if v.get("type") == "title":
            out["title"] = extract_plain_text(v.get("title", []))
            break
    
    out["ngay_hoan_thanh"] = safe_date(props, GOAL_PROP_END)
    out["tong_nhiem_vu"] = safe_rollup(props, GOAL_PROP_TOTAL_TASKS) or 0
    out["da_hoan_thanh"] = safe_rollup(props, GOAL_PROP_DONE_TASKS) or 0
    out["hoan_tuan_nay"] = safe_rollup(props, GOAL_PROP_DONE_WEEK) or 0
    
    # Calculate progress
    if out["tong_nhiem_vu"] > 0:
        out["progress_pct"] = int(round(out["da_hoan_thanh"] / out["tong_nhiem_vu"] * 100))
    else:
        prog = safe_formula(props, GOAL_PROP_PROGRESS)
        if prog:
            try:
                s = str(prog).replace("%", "").strip()
                val = float(s)
                out["progress_pct"] = int(val if val > 1 else val * 100)
            except:
                out["progress_pct"] = 0
        else:
            out["progress_pct"] = 0
    
    return out
def _phan_tich_du_phong(context):
    """Phân tích dự phòng khi AI không hoạt động"""
    return f"""🔍 **ĐÁNH GIÁ**
Hiệu suất tuần này: {context['completion_rate']:.0f}%. {'Tốt' if context['completion_rate'] >= 70 else 'Cần cải thiện'}.

⚡ **PHÁT HIỆN**
Chênh lệch tốc độ: {context['required_velocity'] - context['goal_velocity']:+.1f} tasks/tuần.
{'Cần tăng tốc để đạt mục tiêu đúng hạn.' if context['required_velocity'] > context['goal_velocity'] else 'Đang đúng tiến độ.'}

🎯 **HÀNH ĐỘNG**
- Tuần này: Hoàn thành {int(context['required_velocity'])} tasks
- Tập trung: Xử lý {context['overdue_unfinished']} tasks quá hạn trước
- Ưu tiên: Tasks có giá trị cao của mục tiêu chính

💪 **ĐỘNG LỰC**
Từng bước nhỏ mỗi ngày. Kiên trì quan trọng hơn bùng nổ."""
# ============================================================================
# AI ENGINE - LUÔN CHẠY, PHÂN TÍCH SÂU VÀ THỰC TẾ
# ============================================================================

@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
def call_gpt(messages, temperature=0.75, max_tokens=2500):
    if not OPENAI_API_KEY:
        raise Exception("Missing OPENAI_API_KEY")

    print("[AI] Calling OpenAI...")

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens
    )

    return resp.choices[0].message.content.strip()

def ai_strategic_weekly_analysis(context):
    """
    AI PHÂN TÍCH CHIẾN LƯỢC TUẦN - THUẦN TIẾNG VIỆT
    Đảm bảo luôn chạy và phân tích sâu
    """
    
    # Tính toán chi tiết
    ngay_con_lai = context['days_remaining']
    tuan_con_lai = max(1, ceil(ngay_con_lai / 7))
    tasks_con_lai = context['tasks_remaining']
    toc_do_hien_tai = context['goal_velocity']
    toc_do_can_thiet = context['required_velocity']
    chenh_lech = toc_do_can_thiet - toc_do_hien_tai
    
    prompt = f"""Bạn là cố vấn chiến lược cá nhân, chuyên về đạt mục tiêu và tối ưu năng suất.

📊 TÌNH HÌNH TUẦN VỪA QUA:

**Công việc:**
- Tổng số tasks: {context['total_tasks']}
- Hoàn thành đúng hạn: {context['completed_ontime']}
- Hoàn thành trễ: {context.get('completed_late', 0)}
- Quá hạn chưa làm: {context['overdue_unfinished']}
- Tỷ lệ hoàn thành: {context['completion_rate']:.1f}%

**Mục tiêu: "{context['goal_title']}"**
- Tiến độ hiện tại: {context['goal_progress']}% ({context['goal_done']}/{context['goal_total']} tasks)
- Còn lại: {tasks_con_lai} tasks trong {ngay_con_lai} ngày ({tuan_con_lai} tuần)
- Tốc độ tuần này: {toc_do_hien_tai} tasks/tuần
- Tốc độ cần đạt: {toc_do_can_thiet:.1f} tasks/tuần
- Chênh lệch: {chenh_lech:+.1f} tasks/tuần {'⚠️ CẦN TĂNG TỐC!' if chenh_lech > 0 else '✅ Đang đúng tiến độ'}

**Phân bổ công việc trong tuần:**
{context['workload_distribution']}

**Vấn đề phát hiện:**
{context['detected_issues']}

---

YÊU CẦU: Phân tích sâu và đưa ra chiến lược CỤ THỂ để đạt mục tiêu đúng hạn.

HÃY TRẢ LỜI THEO FORMAT (QUAN TRỌNG - GIỮ ĐÚNG CẤU TRÚC):

🔍 **ĐÁNH GIÁ THỰC TRẠNG**
[Phân tích 2-3 câu về tình hình: đang đi đúng hướng không? Điểm mạnh và yếu?]

⚡ **PHÁT HIỆN QUAN TRỌNG**
[1-2 insight sâu sắc về performance - vấn đề cốt lõi cần giải quyết NGAY]

🎯 **CHIẾN LƯỢC 3 TUẦN TỚI** (mục tiêu tăng {30}% tiến độ)
- **Tuần 1**: [Mục tiêu cụ thể + số tasks cần hoàn thành]
- **Tuần 2**: [Mục tiêu + cách tăng tốc]
- **Tuần 3**: [Giai đoạn hoàn thiện + dự phòng]

🔥 **4 HÀNH ĐỘNG NGAY TUẦN NÀY**
1. [Hành động #1 - ưu tiên cao nhất, cụ thể]
2. [Hành động #2 - tăng tốc độ làm việc]
3. [Hành động #3 - giảm điểm nghẽn]
4. [Hành động #4 - quản lý rủi ro]

💪 **LỜI ĐỘNG VIÊN**
[2-3 câu động lực chân thực, phù hợp tình huống. Tạo năng lượng để thực hiện]

LƯU Ý:
- Thẳng thắn, không lý thuyết suông
- Đưa ra số liệu cụ thể (bao nhiêu tasks mỗi ngày)
- Hành động phải thực hiện được trong 1-2 ngày
- Giọng điệu: người đi trước, có kinh nghiệm
"""

    try:
        return call_gpt([
            {
                "role": "system", 
                "content": "Bạn là chuyên gia tư vấn chiến lược chuyên về đạt mục tiêu. Trả lời bằng tiếng Việt, thẳng thắn và có thể hành động được."
            },
            {"role": "user", "content": prompt}
        ], temperature=0.75, max_tokens=2000)
    except Exception as e:
        print(f"[LỖI] AI phân tích tuần thất bại: {e}")
        return _phan_tich_du_phong(context)

def ai_tactical_weekly_plan(next_tasks, goal, context):
    """
    AI LẬP KẾ HOẠCH TUẦN - THUẦN TIẾNG VIỆT
    Tập trung: Làm GÌ, KHI NÀO, THẾ NÀO
    """
    
    tom_tat_tasks = []
    for t in next_tasks[:25]:
        tom_tat_tasks.append({
            "tieu_de": get_title(t)[:60],
            "han": str(get_date_start(t, PROP_DUE).date()) if get_date_start(t, PROP_DUE) else "Chưa rõ",
            "uu_tien": get_select_name(t, PROP_PRIORITY) or "Trung bình"
        })
    
    prompt = f"""Bạn là chuyên gia lập kế hoạch thực thi, thiết kế kế hoạch khả thi cao.

📋 **CÔNG VIỆC TUẦN TỚI** ({len(next_tasks)} tasks):
{json.dumps(tom_tat_tasks, ensure_ascii=False, indent=2)}

🎯 **MỤC TIÊU & ĐIỀU KIỆN:**
- Mục tiêu: {goal['title']}
- Tasks còn lại: {goal['tong_nhiem_vu'] - goal['da_hoan_thanh']}
- Tốc độ cần đạt: {context['required_velocity']:.1f} tasks/tuần
- Hiệu suất tuần trước: {context['completion_rate']:.0f}%

---

TẠO KẾ HOẠCH THỰC HIỆN THEO FORMAT:

📅 **KẾ HOẠCH CHI TIẾT THEO NGÀY**

**Thứ 2-3: XÂY DỰNG MOMENTUM** (Mục tiêu: {int(context['required_velocity'] * 0.4)} tasks)
[Liệt kê 2-3 tasks cụ thể, bắt đầu với việc dễ để tạo động lực]

**Thứ 4-5: GIAI ĐOẠN CAO ĐIỂM** (Mục tiêu: {int(context['required_velocity'] * 0.4)} tasks)
[Tasks quan trọng và khó nhất, làm khi năng lượng cao]

**Thứ 6: HOÀN THIỆN** (Mục tiêu: {int(context['required_velocity'] * 0.2)} tasks)
[Kết thúc, hoàn thiện, chuẩn bị review]

**Thứ 7 - Chủ nhật: ĐÁNH GIÁ & CHUẨN BỊ**
[Đánh giá tuần + chuẩn bị cho tuần sau]

🎯 **3 MỐC QUAN TRỌNG PHẢI ĐẠT**
1. [Đến Thứ 3]: [Mốc cụ thể]
2. [Đến Thứ 5]: [Mốc cụ thể]
3. [Đến Thứ 6]: [Mốc cụ thể]

⚠️ **QUẢN LÝ RỦI RO**
- Rủi ro #1: [Cụ thể] → Phòng tránh: [Hành động cụ thể]
- Rủi ro #2: [Cụ thể] → Phòng tránh: [Hành động cụ thể]

⏰ **ĐỀ XUẤT PHÂN BỔ THỜI GIAN**
- 09:00-12:00: [Loại công việc]
- 14:00-17:00: [Loại công việc]
- Buổi tối: [Loại công việc]

YÊU CẦU:
- Thực tế với khối lượng công việc
- Để dự phòng cho việc bất ngờ
- Tạo momentum tăng dần (dễ → khó → hoàn thiện)
"""

    try:
        return call_gpt([
            {
                "role": "system", 
                "content": "Bạn là chuyên gia lập kế hoạch chiến thuật. Tạo kế hoạch thực tế, có thể thực hiện. Trả lời bằng tiếng Việt."
            },
            {"role": "user", "content": prompt}
        ], temperature=0.7, max_tokens=2200)
    except Exception as e:
        print(f"[LỖI] AI lập kế hoạch thất bại: {e}")
        return f"⚠️ Tuần tới cần hoàn thành khoảng {int(context['required_velocity'])} tasks. Kế hoạch chi tiết đang được tạo."

def ai_monthly_insights(monthly_context):
    """
    AI PHÂN TÍCH BÁO CÁO THÁNG
    - Review tháng vừa qua
    - Lessons learned
    - Đề xuất cho tháng tới
    """
    
    prompt = f"""Bạn là executive coach chuyên về long-term goal achievement.

📊 KẾT QUẢ THÁNG VỪA QUA:

**Performance tổng thể:**
• Việc hằng ngày hoàn thành: {monthly_context['daily_done']}
• Quá hạn đã xử lý: {monthly_context['overdue_completed']}
• Quá hạn chưa xử lý: {monthly_context['overdue_remaining']}

**Tiến độ mục tiêu:**
{monthly_context['goals_summary']}

**Trends:**
• So với tháng trước: {monthly_context['trend']}
• Completion velocity: {monthly_context.get('avg_completion', 'N/A')}

---

HÃY PHẢN HỒI THEO FORMAT:

📈 **REVIEW THÁNG VỪA QUA**
[2-3 câu đánh giá tổng thể: Highlights và lowlights]

💡 **3 LESSONS LEARNED**
1. [Bài học #1 từ data]
2. [Bài học #2 về patterns]
3. [Bài học #3 về execution]

🎯 **ĐỀ XUẤT CHO THÁNG TỚI**
• Focus area: [1-2 lĩnh vực cần tập trung]
• Adjustment: [Điều chỉnh cần làm]
• New habits: [Thói quen mới nên thử]

🔥 **CHALLENGE THÁNG TỚI**
[1 challenge cụ thể để push performance lên tầm cao mới]

Yêu cầu:
- Strategic thinking (nhìn dài hạn)
- Actionable insights
- Dựa trên data thực tế
- Tone: executive mentor
"""

    try:
        return call_gpt([
            {"role": "system", "content": "You are an executive coach specializing in monthly performance review and strategic planning. Answer in Vietnamese."},
            {"role": "user", "content": prompt}
        ], temperature=0.8, max_tokens=1500)
    except Exception as e:
        print(f"[ERROR] AI monthly insights failed: {e}")
        return _monthly_fallback(monthly_context)

def _monthly_fallback(context):
    """Fallback cho AI monthly"""
    return f"""📈 **REVIEW**
Tháng này hoàn thành {context['daily_done']} việc hằng ngày. {'Tiến bộ tốt!' if context['daily_done'] > 20 else 'Cần cải thiện.'}

💡 **LESSONS**
1. Duy trì consistency quan trọng hơn intensity
2. Focus vào mục tiêu quan trọng nhất
3. Buffer time cho unexpected tasks

🎯 **THÁNG TỚI**
• Focus: Tăng completion rate lên >75%
• Thử: Time blocking buổi sáng
• Goal: +20% tasks cho mục tiêu chính

🔥 **CHALLENGE**
Hoàn thành 30+ tasks hằng ngày tháng tới!"""

# ============================================================================
# JOB WEEKLY - VERSION MỚI: LUÔN GỌI AI, PHÂN TÍCH SÂU
# ============================================================================

def job_weekly():
    """BÁO CÁO TUẦN với AI PHÂN TÍCH SÂU - LUÔN CHẠY"""
    
    print(f"\n{'='*70}")
    print(f"[WEEKLY REPORT] Started at {datetime.datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*70}\n")

    today = datetime.datetime.now(TZ).date()
    week_start, week_end = week_range(today)

    # ====================================================================
    # STEP 1: THU THẬP TẤT CẢ TASKS TRONG TUẦN (cả done và chưa done)
    # ====================================================================
    print("[1/5] Collecting data...")
    
    # Query CẢ done và chưa done để tính completion rate
    all_tasks = notion_query(
        REMIND_DB,
        {
            "or": [
                {
                    "property": PROP_DUE,
                    "date": {
                        "on_or_after": week_start.isoformat(),
                        "on_or_before": week_end.isoformat()
                    }
                },
                {
                    "property": PROP_DUE,
                    "date": {"before": week_start.isoformat()}
                }
            ]
        }
    ) or []

    print(f"      → Found {len(all_tasks)} tasks in week range")

    # Phân tích performance (ĐÚNG – KHÔNG CRASH)
    completed_ontime = 0
    completed_late = 0
    overdue_pending = 0

    for task in all_tasks:
        is_done = get_checkbox(task, PROP_DONE)
        due_dt = get_date_start(task, PROP_DUE)

        if not due_dt:
            continue

        if is_done:
            completed_dt = _parse_completed_datetime_from_page(task)

            if completed_dt:
                completed_date = (
                    completed_dt.date()
                    if isinstance(completed_dt, datetime.datetime)
                    else completed_dt
                )

                if completed_date <= due_dt.date():
                    completed_ontime += 1
                else:
                    completed_late += 1
            else:
                # Không có completed date → fallback an toàn
                completed_ontime += 1

        else:
            if due_dt.date() < today:
                overdue_pending += 1

    total_tasks = len(all_tasks)
    completed_total = completed_ontime + completed_late
    completion_rate = (completed_total / total_tasks * 100) if total_tasks > 0 else 0

    # ---------- WORKLOAD BY DAY (TIẾNG VIỆT) ----------
    weekday_map = {
        0: "Thứ 2",
        1: "Thứ 3",
        2: "Thứ 4",
        3: "Thứ 5",
        4: "Thứ 6",
        5: "Thứ 7",
        6: "Chủ nhật"
    }

    workload_by_day = defaultdict(int)
    for t in all_tasks:
        due = get_date_start(t, PROP_DUE)
        if due:
            workload_by_day[weekday_map[due.weekday()]] += 1

    workload_distribution = "\n".join(
        f"  • {day}: {count} tasks"
        for day, count in workload_by_day.items()
    ) or "  • Không có dữ liệu"


    # ====================================================================
    # STEP 2: PHÂN TÍCH MỤC TIÊU
    # ====================================================================
    print("[2/5] Analyzing goal...")
    
    goals = notion_query(GOALS_DB) or []
    if not goals:
        print("      ⚠️ No goals found!")
        send_telegram("⚠️ Không tìm thấy mục tiêu để phân tích. Hãy tạo goal trong Notion.")
        return
    
    # Pick goal đang active
    active_goals = [
        read_goal_properties(g)
        for g in goals
        if read_goal_properties(g)['progress_pct'] < 100
    ]

    active_goals.sort(
        key=lambda g: g.get("ngay_hoan_thanh") or datetime.date.max
    )

    target_goal = active_goals[0] if active_goals else None
  
    if not target_goal:
        print("      ⚠️ No active goal!")
        send_telegram("✅ Tất cả mục tiêu đã hoàn thành! Time to celebrate 🎉")
        return
    
    print(f"      → Target: {target_goal['title']} ({target_goal['progress_pct']}%)")
    
    # Tính toán velocity
    days_remaining = (target_goal['ngay_hoan_thanh'] - today).days if target_goal['ngay_hoan_thanh'] else 30
    weeks_remaining = max(1, ceil(days_remaining / 7))
    tasks_remaining = max(0, target_goal['tong_nhiem_vu'] - target_goal['da_hoan_thanh'])
    required_velocity = round(tasks_remaining / weeks_remaining, 2)
    
    print(f"      → Need {required_velocity} tasks/week for {weeks_remaining} weeks")
    
    # ====================================================================
    # STEP 3: PHÁT HIỆN VẤN ĐỀ
    # ====================================================================
    print("[3/5] Detecting issues...")
    
    issues = []
    if overdue_pending >= 3:
        issues.append(f"⚠️ {overdue_pending} tasks quá hạn - Risk cao!")
    if completion_rate < 50:
        issues.append(f"📉 Completion rate thấp ({completion_rate:.0f}%) - Cần review workload")
    if target_goal and target_goal.get('hoan_tuan_nay', 0) == 0:
        issues.append("⛔ Chưa complete task nào cho goal - Mất focus")
    if required_velocity > target_goal['hoan_tuan_nay'] * 2:
        issues.append(f"🚨 Cần tăng velocity gấp đôi ({required_velocity:.1f} vs {target_goal['hoan_tuan_nay']})")
    
    if not issues:
        issues.append("✅ Không phát hiện vấn đề nghiêm trọng")
    
    # ====================================================================
    # STEP 4: AI PHÂN TÍCH CHIẾN LƯỢC - LUÔN CHẠY
    # ====================================================================
    print("[4/5] Running AI strategic analysis...")
    
    analysis_context = {
        'total_tasks': total_tasks,
        'completed_ontime': completed_ontime,
        'completed_late': completed_late,
        'overdue_unfinished': overdue_pending,
        'completion_rate': completion_rate,

        'goal_title': target_goal['title'] if target_goal else "Không có mục tiêu",
        'goal_progress': target_goal.get('progress_pct', 0) if target_goal else 0,
        'goal_done': target_goal.get('da_hoan_thanh', 0) if target_goal else 0,
        'goal_total': target_goal.get('tong_nhiem_vu', 0) if target_goal else 0,
        'goal_velocity': target_goal.get('hoan_tuan_nay', 0) if target_goal else 0,

        'required_velocity': required_velocity,
        'days_remaining': days_remaining,
        'tasks_remaining': tasks_remaining,
        'workload_distribution': workload_distribution,
        'detected_issues': "\n".join(issues)
    }

    try:
        ai_analysis = ai_strategic_weekly_analysis(analysis_context)
        print("      ✓ AI analysis completed")
    except Exception as e:
        print(f"      ✗ AI analysis failed: {e}")
        ai_analysis = _phan_tich_du_phong(analysis_context)
    
    # ====================================================================
    # STEP 5: AI LẬP KẾ HOẠCH - LUÔN CHẠY
    # ====================================================================
    print("[5/5] Generating AI tactical plan...")
    
    next_week_start = week_end + datetime.timedelta(days=1)
    next_week_end = next_week_start + datetime.timedelta(days=6)
    
    next_week_tasks = notion_query(
        REMIND_DB,
        {
            "and": [
                {
                    "property": PROP_DUE,
                    "date": {
                        "on_or_after": next_week_start.isoformat(),
                        "on_or_before": next_week_end.isoformat()
                    }
                },
                {"property": PROP_DONE, "checkbox": {"equals": False}}
            ]
        }
    ) or []
    
    print(f"      → Found {len(next_week_tasks)} tasks for next week")
    
    try:
        ai_plan = ai_tactical_weekly_plan(next_week_tasks, target_goal, analysis_context)
        print("      ✓ AI plan completed")
    except Exception as e:
        print(f"      ✗ AI plan failed: {e}")
        ai_plan = f"⚠️ Tuần tới cần complete ~{int(required_velocity)} tasks. AI plan sẽ được tạo khi system ổn định."
    
    # ====================================================================
    # STEP 6: GỬI BÁO CÁO
    # ====================================================================
    progress_bar = "█" * (target_goal['progress_pct'] // 10) + "░" * (10 - target_goal['progress_pct'] // 10)
    
    message = f"""
📊 <b>BÁO CÁO TUẦN — {week_start.strftime('%d/%m')} đến {week_end.strftime('%d/%m/%Y')}</b>

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
<b>📈 HIỆU SUẤT TUẦN VỪA QUA</b>

<b>Công việc:</b>
  ✅ Hoàn thành: <b>{completed_ontime}</b>/{total_tasks}
  🆘 Quá hạn chưa xử lý: {overdue_pending}
  📊 Completion rate: <b>{completion_rate:.1f}%</b>

<b>Mục tiêu: {target_goal['title']}</b>
  📈 Progress: <b>{target_goal['progress_pct']}%</b> [{progress_bar}]
  ⚡ Velocity tuần này: {target_goal['hoan_tuan_nay']} tasks
  🎯 Velocity cần thiết: <b>{required_velocity:.1f} tasks/tuần</b>
  ⏰ Thời gian còn lại: {days_remaining} ngày ({weeks_remaining} tuần)
  📦 Tasks còn lại: {tasks_remaining}/{target_goal['tong_nhiem_vu']}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
<b>🤖 PHÂN TÍCH CHIẾN LƯỢC AI</b>

{ai_analysis}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
<b>📅 KẾ HOẠCH TUẦN TỚI</b>

<b>Công việc tuần tới:</b> {len(next_week_tasks)} tasks

{ai_plan}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
<i>🤖 AI-Powered Strategic Report • {datetime.datetime.now(TZ).strftime('%H:%M %d/%m/%Y')}</i>
"""
    
    send_telegram_long(message.strip())
    
    print(f"\n✅ Weekly report sent successfully!")
    print(f"{'='*70}\n")

# ============================================================================
# JOB DAILY - SIMPLIFIED VERSION
# ============================================================================

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
    LAST_TASKS["daily"] = [p.get("id") for p in tasks if p and isinstance(p, dict)]


# ============================================================================
# PHẦN 2: HÀM job_monthly MỚI - THAY THẾ HOÀN TOÀN
# ============================================================================

def job_monthly():
    """
    BÁO CÁO THÁNG với AI INSIGHTS
    - Giữ nguyên logic cũ
    - Thêm AI phân tích strategic
    """
    today = datetime.datetime.now(TZ).date()
    mstart, mend = month_range(today)
    print(f"[INFO] job_monthly start for {mstart} -> {mend}")

    # ========================================================================
    # PHẦN 1: LOGIC CŨ - GIỮ NGUYÊN
    # ========================================================================
    filters_done = [{"property": PROP_DONE, "checkbox": {"equals": True}}]
    if PROP_ACTIVE:
        filters_done.insert(0, {"property": PROP_ACTIVE, "checkbox": {"equals": True}})

    try:
        done_pages = notion_query(REMIND_DB, {"and": filters_done})
        print(f"[DBG] job_monthly: fetched {len(done_pages)} done pages")
    except Exception as e:
        print("[WARN] job_monthly: notion_query failed:", e)
        done_pages = []

    # Tính tasks hoàn thành trong tháng
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

    # Đếm việc hằng ngày
    daily_month_done = 0
    for p, comp_date in done_this_month:
        try:
            ttype = get_select_name(p, PROP_TYPE) or ""
            if "hằng" in ttype.lower():
                daily_month_done += 1
        except:
            continue

    # Đếm overdue done
    overdue_done = 0
    for p, comp_date in done_this_month:
        try:
            due = get_date_start(p, PROP_DUE)
            if due and comp_date and comp_date > due.date():
                overdue_done += 1
        except:
            continue

    # Đếm overdue chưa xử lý
    filters_overdue = [
        {"property": PROP_DONE, "checkbox": {"equals": False}},
        {"property": PROP_DUE, "date": {"before": today.isoformat()}}
    ]
    if PROP_ACTIVE:
        filters_overdue.insert(0, {"property": PROP_ACTIVE, "checkbox": {"equals": True}})
    
    try:
        q_overdue = notion_query(REMIND_DB, {"and": filters_overdue})
        overdue_remaining = len(q_overdue)
    except Exception as e:
        print("[WARN] overdue query failed:", e)
        overdue_remaining = 0

    # Tổng hợp mục tiêu
    goals_summary = []
    goals_text = []
    
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
                continue

            total = ginfo.get("tong_nhiem_vu", 0)
            done_total = ginfo.get("da_hoan_thanh", 0)

            monthly_done = ginfo.get("nhiem_vu_hoan_thang_rollup") or 0
            progress_pct = ginfo.get("progress_pct") or 0

            gs = {
                "name": ginfo.get("title") or "(no title)",
                "progress": int(progress_pct),
                "done": done_total,
                "total": total,
                "monthly_done": monthly_done
            }
            goals_summary.append(gs)
            
            # Text cho AI
            goals_text.append(f"  • {gs['name']}: {gs['progress']}% ({gs['done']}/{gs['total']}) - Tháng này: +{gs['monthly_done']} tasks")

    # ========================================================================
    # PHẦN 2: AI INSIGHTS - MỚI THÊM
    # ========================================================================
    print("[AI] Generating monthly insights...")
    
    monthly_context = {
        'daily_done': daily_month_done,
        'overdue_completed': overdue_done,
        'overdue_remaining': overdue_remaining,
        'goals_summary': "\n".join(goals_text) if goals_text else "  • Chưa có mục tiêu",
        'trend': 'Đang phát triển' if daily_month_done > 20 else 'Cần cải thiện',
        'avg_completion': f"{daily_month_done/30:.1f} tasks/ngày" if daily_month_done > 0 else "N/A"
    }
    
    try:
        ai_insights = ai_monthly_insights(monthly_context)
        print("[AI] Monthly insights generated")
    except Exception as e:
        print(f"[ERROR] AI monthly insights failed: {e}")
        ai_insights = _monthly_fallback(monthly_context)

    # ========================================================================
    # PHẦN 3: BUILD MESSAGE - NÂNG CẤP
    # ========================================================================
    lines = [f"📅 <b>BÁO CÁO THÁNG {today.strftime('%m/%Y')}</b>", ""]
    lines.append(f"• ✔ Việc hằng ngày hoàn thành: <b>{daily_month_done}</b>")
    lines.append(f"• ⏳ Quá hạn đã xử lý: {overdue_done}")
    lines.append(f"• 🆘 Quá hạn chưa xử lý: {overdue_remaining}")
    lines.append("")
    lines.append("🎯 <b>Tiến độ mục tiêu:</b>")
    
    for g in sorted(goals_summary, key=lambda x: -x['progress'])[:8]:
        bar = "█" * (g['progress'] // 10) + "░" * (10 - g['progress'] // 10)
        lines.append(f"• {g['name']}")
        lines.append(f"  → {g['progress']}% ({g['done']}/{g['total']}) [{bar}]")
        lines.append(f"  → Tháng này: +{g['monthly_done']} tasks")
    
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("<b>🤖 AI STRATEGIC INSIGHTS</b>")
    lines.append("")
    lines.append(ai_insights)
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("<i>💪 Tháng mới, động lực mới!</i>")
    lines.append("")
    lines.append(f"<i>🤖 Generated by AI • {datetime.datetime.now(TZ).strftime('%H:%M %d/%m/%Y')}</i>")

    send_telegram("\n".join(lines).strip())
    print(f"[INFO] job_monthly sent with AI insights")

# ============================================================================
# TELEGRAM WEBHOOK HANDLERS
# ============================================================================

@app.route("/webhook", methods=["POST"])
def webhook():
    global LAST_TASKS
    try:
        update = request.get_json(silent=True) or {}
        message = update.get("message", {}) or {}
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = (message.get("text", "") or "").strip()
        
        if not text.startswith("/"):
            return jsonify({"ok": True}), 200
        
        # /check - show tasks
        if text.lower() == "/check":
            today = datetime.datetime.now(TZ).date()
            week_start, week_end = week_range(today)
            
            tasks = notion_query(
                REMIND_DB,
                {
                    "and": [
                        {"property": PROP_DONE, "checkbox": {"equals": False}},
                        {"or": [
                            {"property": PROP_DUE, "date": {
                                "on_or_after": week_start.isoformat(),
                                "on_or_before": week_end.isoformat()
                            }},
                            {"property": PROP_DUE, "date": {"before": today.isoformat()}}
                        ]}
                    ]
                }
            ) or []
            
            if not tasks:
                send_telegram("🎉 Không có nhiệm vụ pending!")
                return jsonify({"ok": True}), 200
            
            lines = [f"📋 <b>Tasks tuần này</b> ({len(tasks)})\n"]
            for i, p in enumerate(tasks[:20], 1):
                title = get_title(p)
                pri = get_select_name(p, PROP_PRIORITY)
                emoji = priority_emoji(pri)
                d = overdue_days(p)
                status = f"Trễ {d}d" if d and d > 0 else f"Còn {abs(d)}d" if d else ""
                lines.append(f"{i}. {emoji} {title} {status}")
            
            LAST_TASKS[chat_id] = [p.get("id") for p in tasks[:20]]
            send_telegram("\n".join(lines))
            return jsonify({"ok": True}), 200
        
        # /done.N - mark done
        elif text.lower().startswith("/done."):
            parts = text.split(".", 1)
            if len(parts) < 2 or not parts[1].isdigit():
                send_telegram("❌ Dùng: /done.1")
                return jsonify({"ok": True}), 200
            
            n = int(parts[1])
            task_list = LAST_TASKS.get(chat_id, [])
            
            if n < 1 or n > len(task_list):
                send_telegram("❌ Số không hợp lệ")
                return jsonify({"ok": True}), 200
            
            page_id = task_list[n - 1]
            notion_update_page(page_id, {
                PROP_DONE: {"checkbox": True},
                PROP_COMPLETED: {"date": {"start": datetime.datetime.now(TZ).isoformat()}}
            })
            
            send_telegram(f"✅ Done task #{n}!")
            return jsonify({"ok": True}), 200
        
        send_telegram("❓ Lệnh: /check, /done.N")
        return jsonify({"ok": True}), 200
        
    except Exception as e:
        print(f"[ERROR] Webhook: {e}")
        return jsonify({"ok": True}), 200

@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

@app.route("/debug/run_weekly", methods=["POST", "GET"])
def debug_run_weekly():
    secret = os.getenv("MANUAL_TRIGGER_SECRET", "")
    if secret:
        token = request.args.get("token", "")
        if token != secret:
            return jsonify({"error": "forbidden"}), 403
    try:
        job_weekly()
        return jsonify({"ok": True, "msg": "Weekly report executed"}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ============================================================================
# SCHEDULER
# ============================================================================

def start_scheduler():
    sched = BackgroundScheduler(timezone=TIMEZONE)
    
    # Daily reminder
    sched.add_job(job_daily, 'cron', hour=REMIND_HOUR, minute=REMIND_MINUTE, id='daily')
    print(f"  → Daily: {REMIND_HOUR:02d}:{REMIND_MINUTE:02d}")
    
    # Weekly report (Sunday evening)
    sched.add_job(job_weekly, 'cron', day_of_week='sun', hour=WEEKLY_HOUR, minute=0, id='weekly')
    print(f"  → Weekly: Sunday {WEEKLY_HOUR:02d}:00")
    
    # Monthly report
    def monthly_check():
        tomorrow = datetime.datetime.now(TZ).date() + datetime.timedelta(days=1)
        if tomorrow.day == 1:
            job_monthly()
    
    sched.add_job(monthly_check, 'cron', hour=MONTHLY_HOUR, minute=0, id='monthly_check')
    print(f"  → Monthly: Day 1 at {MONTHLY_HOUR:02d}:00")
    
    sched.start()
    return sched

def set_telegram_webhook():
    WEBHOOK_URL = os.getenv("WEBHOOK_URL")

    if TELEGRAM_TOKEN and WEBHOOK_URL:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
                data={"url": WEBHOOK_URL},
                timeout=10
            )
            print(f"  → Webhook set: {r.status_code}")
        except Exception as e:
            print(f"  → Webhook error: {e}")
    else:
        print("  → Webhook not set (missing TELEGRAM_TOKEN or WEBHOOK_URL)")

# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    print("\n" + "="*70)
    print("🤖 AI-POWERED WEEKLY REPORT SYSTEM")
    print("="*70 + "\n")
    
    # Validate config
    if not NOTION_TOKEN or not REMIND_DB:
        print("❌ FATAL: Missing NOTION_TOKEN or REMIND_NOTION_DATABASE")
        raise SystemExit(1)
    
    if not OPENAI_API_KEY:
        print("⚠️  WARNING: Missing OPENAI_API_KEY - AI features will use fallback")
    
    if not GOALS_DB:
        print("⚠️  WARNING: Missing GOALS_NOTION_DATABASE - Cannot track goals")
    
    print("✓ Configuration loaded")
    print(f"  → Notion DB: {REMIND_DB[:12]}...")
    print(f"  → Goals DB: {GOALS_DB[:12] if GOALS_DB else 'NOT SET'}...")
    print(f"  → OpenAI: {'ENABLED' if OPENAI_API_KEY else 'DISABLED'}")
    print(f"  → Telegram: {'ENABLED' if TELEGRAM_TOKEN else 'DISABLED'}")
    print()
    
    # Setup webhook if needed
    if TELEGRAM_TOKEN:
        set_telegram_webhook()
    
    # Start scheduler
    print("Starting scheduler...")
    start_scheduler()
    print()
    
    # Run on start if enabled
    if RUN_ON_START:
        print("🚀 Running initial job_daily...")
        try:
            job_daily()
        except Exception as e:
            print(f"❌ Initial run failed: {e}")
    
    # Decide run mode
    BACKGROUND_WORKER = os.getenv("BACKGROUND_WORKER", "true").lower() in ("1", "true", "yes")
    
    if BACKGROUND_WORKER:
        print("="*70)
        print("🔄 Running in BACKGROUND WORKER mode")
        print("   Service will keep running for scheduled jobs")
        print("="*70 + "\n")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("\n👋 Shutting down gracefully...")
    else:
        port = int(os.getenv("PORT", 5000))
        print("="*70)
        print(f"🌐 Starting Flask server on port {port}")
        print("="*70 + "\n")
        app.run(host="0.0.0.0", port=port, threaded=True)

