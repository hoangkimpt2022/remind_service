#!/usr/bin/env python3
"""
🏋️ KỶ LUẬT 3 VIỆC/NGÀY — Telegram Bot + Notion
Deploy: Render (Flask + APScheduler)
Env vars: TELEGRAM_TOKEN, NOTION_TOKEN, NOTION_DB_ID, CHAT_ID, WEBHOOK_URL
"""

import os
import json
import datetime
import requests
import pytz
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════
app = Flask(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip()
NOTION_DB_ID = os.getenv("NOTION_DB_ID", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
TZ = pytz.timezone("Asia/Ho_Chi_Minh")

# Notion property names (khớp DB của Sếp)
P_TITLE = "Việc cần làm"
P_DATE = "Ngày"
P_STATUS = "trạng thái"
P_ORDER = "STT/ngày"
P_STREAK = "Chuỗi"

# Status values
S_DOING = "Đang làm"
S_DONE = "Xong"
S_OVERDUE = "Trễ hạn"

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

# State: chờ Sếp nhập 3 việc
WAITING_TASKS = {}  # {chat_id: True}

# ═══════════════════════════════════════════════════════════════
# NOTION HELPERS
# ═══════════════════════════════════════════════════════════════

def notion_query(filter_payload=None):
    """Query Notion DB"""
    url = f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query"
    payload = {"page_size": 100}
    if filter_payload:
        payload["filter"] = filter_payload
    try:
        r = requests.post(url, headers=NOTION_HEADERS, json=payload, timeout=15)
        if r.status_code != 200:
            print(f"[NOTION ERR] {r.status_code}: {r.text[:300]}")
            return []
        return r.json().get("results", [])
    except Exception as e:
        print(f"[NOTION ERR] {e}")
        return []


def notion_create(title, date_str, order, streak=0):
    """Tạo 1 task mới trong Notion"""
    url = "https://api.notion.com/v1/pages"
    payload = {
        "parent": {"database_id": NOTION_DB_ID},
        "properties": {
            P_TITLE: {"title": [{"text": {"content": title}}]},
            P_DATE: {"date": {"start": date_str}},
            P_STATUS: {"select": {"name": S_DOING}},
            P_ORDER: {"number": order},
            P_STREAK: {"number": streak},
        },
    }
    try:
        r = requests.post(url, headers=NOTION_HEADERS, json=payload, timeout=15)
        return r.status_code == 200
    except Exception as e:
        print(f"[NOTION CREATE ERR] {e}")
        return False


def notion_update(page_id, props):
    """Update properties của 1 page"""
    url = f"https://api.notion.com/v1/pages/{page_id}"
    try:
        r = requests.patch(url, headers=NOTION_HEADERS, json={"properties": props}, timeout=15)
        return r.status_code == 200
    except Exception as e:
        print(f"[NOTION UPDATE ERR] {e}")
        return False


def get_title(page):
    """Lấy title từ page"""
    prop = page.get("properties", {}).get(P_TITLE, {})
    titles = prop.get("title", [])
    return "".join(t.get("plain_text", "") for t in titles).strip() or "—"


def get_status(page):
    sel = page.get("properties", {}).get(P_STATUS, {}).get("select")
    return sel.get("name", "") if sel else ""


def get_order(page):
    return page.get("properties", {}).get(P_ORDER, {}).get("number") or 0


def today_str():
    return datetime.datetime.now(TZ).strftime("%Y-%m-%d")


def get_today_tasks():
    """Lấy 3 task hôm nay, sorted by order"""
    pages = notion_query({
        "property": P_DATE,
        "date": {"equals": today_str()}
    })
    pages.sort(key=lambda p: get_order(p))
    return pages


def get_tasks_range(start_date, end_date):
    """Lấy tasks trong khoảng ngày"""
    return notion_query({
        "and": [
            {"property": P_DATE, "date": {"on_or_after": start_date}},
            {"property": P_DATE, "date": {"on_or_before": end_date}},
        ]
    })


# ═══════════════════════════════════════════════════════════════
# STREAK LOGIC
# ═══════════════════════════════════════════════════════════════

def calculate_current_streak():
    """
    Đếm streak: bao nhiêu ngày liên tiếp (tính từ hôm qua trở về trước)
    mà cả 3 task đều Xong.
    """
    today = datetime.datetime.now(TZ).date()
    streak = 0
    check_date = today - datetime.timedelta(days=1)

    for _ in range(365):  # max 1 năm
        date_str = check_date.strftime("%Y-%m-%d")
        tasks = notion_query({
            "property": P_DATE,
            "date": {"equals": date_str}
        })

        if len(tasks) == 0:
            break  # ngày không có task → dừng

        all_done = all(get_status(t) == S_DONE for t in tasks) and len(tasks) >= 3
        if all_done:
            streak += 1
            check_date -= datetime.timedelta(days=1)
        else:
            break

    return streak


# ═══════════════════════════════════════════════════════════════
# TELEGRAM HELPERS
# ═══════════════════════════════════════════════════════════════

def tg_send(text, chat_id=None, reply_markup=None):
    """Gửi tin nhắn Telegram"""
    cid = chat_id or CHAT_ID
    if not TELEGRAM_TOKEN or not cid:
        print(f"[TG OFF] {text}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": cid,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[TG ERR] {e}")


def build_review_keyboard(tasks):
    """Tạo inline keyboard cho review tối"""
    buttons = []
    for t in tasks:
        order = get_order(t)
        title = get_title(t)
        status = get_status(t)
        if status != S_DONE:
            emoji = "☐"
            buttons.append([{
                "text": f"✅ {order}. {title}",
                "callback_data": f"done:{t['id']}"
            }])
        # Nếu đã done thì không tạo nút
    return {"inline_keyboard": buttons} if buttons else None


# ═══════════════════════════════════════════════════════════════
# SCHEDULED JOBS
# ═══════════════════════════════════════════════════════════════

def job_morning():
    """9:00 — Hỏi 3 việc"""
    print(f"[JOB] morning {datetime.datetime.now(TZ)}")

    # Check đã có task hôm nay chưa
    existing = get_today_tasks()
    if len(existing) >= 3:
        tg_send(
            "🌅 <b>Sếp ơi, hôm nay đã có 3 việc rồi!</b>\n\n"
            + format_task_list(existing)
            + "\n\n🔥 Chiến thôi nào!"
        )
        return

    WAITING_TASKS[CHAT_ID] = True
    tg_send(
        "🌅 <b>Chào buổi sáng Sếp!</b>\n\n"
        "📝 Hôm nay 3 việc quan trọng nhất là gì?\n"
        "Gửi <b>3 dòng</b>, mỗi dòng 1 việc 👇"
    )


def job_evening():
    """21:00 — Review cuối ngày"""
    print(f"[JOB] evening {datetime.datetime.now(TZ)}")

    tasks = get_today_tasks()
    if not tasks:
        tg_send("🌙 Hôm nay chưa có việc nào được ghi nhận 😶")
        return

    done_count = sum(1 for t in tasks if get_status(t) == S_DONE)

    if done_count >= 3:
        streak = calculate_current_streak()
        tg_send(
            "🌙 <b>Review cuối ngày</b>\n\n"
            + format_task_list(tasks)
            + f"\n\n🎉 <b>Perfect day!</b> 3/3 hoàn thành!"
            + f"\n🔥 Streak: {streak + 1} ngày liên tiếp!"
        )
        return

    # Còn task chưa xong → gửi nút bấm
    text = (
        "🌙 <b>Review cuối ngày!</b>\n\n"
        + format_task_list(tasks)
        + f"\n\n📊 Đã xong: {done_count}/3"
        + "\n\n👇 Bấm nút để đánh dấu xong:"
    )
    kb = build_review_keyboard(tasks)
    tg_send(text, reply_markup=kb)


def job_midnight():
    """0:00 — Xử lý trễ hạn + tính streak"""
    print(f"[JOB] midnight {datetime.datetime.now(TZ)}")

    # Lấy task của NGÀY HÔM QUA (vì đã sang ngày mới)
    yesterday = (datetime.datetime.now(TZ) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    tasks = notion_query({
        "property": P_DATE,
        "date": {"equals": yesterday}
    })

    if not tasks:
        return

    overdue_list = []
    done_count = 0

    for t in tasks:
        if get_status(t) == S_DOING:
            # Chuyển thành Trễ hạn
            notion_update(t["id"], {
                P_STATUS: {"select": {"name": S_OVERDUE}}
            })
            overdue_list.append(get_title(t))
        elif get_status(t) == S_DONE:
            done_count += 1

    streak = calculate_current_streak()

    # Update streak vào tất cả task hôm qua
    for t in tasks:
        notion_update(t["id"], {P_STREAK: {"number": streak}})

    # Gửi báo cáo
    if overdue_list:
        overdue_text = "\n".join(f"  ❌ {v}" for v in overdue_list)
        tg_send(
            f"⏰ <b>Hết ngày!</b>\n\n"
            f"✅ Xong: {done_count}/3\n"
            f"❌ Trễ hạn:\n{overdue_text}\n\n"
            f"💔 Streak bị đứt → <b>0 ngày</b>\n"
            f"Ngày mai làm lại từ đầu nha Sếp! 💪"
        )
    else:
        tg_send(
            f"🎊 <b>PERFECT DAY!</b> 3/3 hoàn thành!\n\n"
            f"🔥 Streak hiện tại: <b>{streak} ngày</b> liên tiếp!\n"
            f"Giữ lửa nha Sếp! 🏆"
        )


def job_weekly():
    """Chủ nhật 20:00 — Báo cáo tuần"""
    print(f"[JOB] weekly {datetime.datetime.now(TZ)}")

    today = datetime.datetime.now(TZ).date()
    week_start = today - datetime.timedelta(days=6)
    start_str = week_start.strftime("%Y-%m-%d")
    end_str = today.strftime("%Y-%m-%d")

    tasks = get_tasks_range(start_str, end_str)

    total = len(tasks)
    done = sum(1 for t in tasks if get_status(t) == S_DONE)
    overdue = sum(1 for t in tasks if get_status(t) == S_OVERDUE)
    doing = sum(1 for t in tasks if get_status(t) == S_DOING)

    pct = (done / total * 100) if total > 0 else 0
    streak = calculate_current_streak()

    # Tính số ngày perfect (3/3)
    perfect_days = 0
    for i in range(7):
        d = (week_start + datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        day_tasks = [t for t in tasks if t.get("properties", {}).get(P_DATE, {}).get("date", {}).get("start") == d]
        if len(day_tasks) >= 3 and all(get_status(t) == S_DONE for t in day_tasks):
            perfect_days += 1

    # Emoji rating
    if pct >= 90:
        rating = "🏆 XUẤT SẮC!"
    elif pct >= 70:
        rating = "💪 KHÁ TỐT!"
    elif pct >= 50:
        rating = "😤 CỐ LÊN!"
    else:
        rating = "😰 NGUY HIỂM!"

    # Progress bar
    bar_len = 14
    filled = int(round(bar_len * pct / 100))
    bar = "█" * filled + "░" * (bar_len - filled)

    tg_send(
        f"📊 <b>BÁO CÁO TUẦN</b>\n"
        f"📅 {week_start.strftime('%d/%m')} → {today.strftime('%d/%m')}\n\n"
        f"[{bar}] {pct:.0f}%\n\n"
        f"✅ Hoàn thành: <b>{done}/{total}</b> task\n"
        f"❌ Trễ hạn: {overdue} task\n"
        f"⭐ Ngày perfect: {perfect_days}/7\n"
        f"🔥 Streak hiện tại: {streak} ngày\n\n"
        f"{rating}\n\n"
        f"{'─' * 20}\n"
        f"💡 <i>Mỗi ngày 3 việc, kỷ luật tạo tự do.</i>"
    )


# ═══════════════════════════════════════════════════════════════
# FORMAT HELPERS
# ═══════════════════════════════════════════════════════════════

def format_task_list(tasks):
    """Format danh sách task đẹp"""
    lines = []
    for t in tasks:
        order = get_order(t)
        title = get_title(t)
        status = get_status(t)

        if status == S_DONE:
            icon = "✅"
        elif status == S_OVERDUE:
            icon = "❌"
        else:
            icon = "⬜"

        lines.append(f"  {icon} {order}. {title}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# TELEGRAM WEBHOOK HANDLER
# ═══════════════════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}

    # ── Callback query (nút bấm) ──
    callback = data.get("callback_query")
    if callback:
        return handle_callback(callback)

    # ── Text message ──
    msg = data.get("message", {})
    text = (msg.get("text") or "").strip()
    chat_id = str(msg.get("chat", {}).get("id", ""))

    if not text or not chat_id:
        return jsonify({"ok": True}), 200

    # Chỉ xử lý từ Sếp
    if chat_id != CHAT_ID:
        return jsonify({"ok": True}), 200

    # ── Commands ──
    cmd = text.lower()

    if cmd == "/start":
        tg_send(
            "🏋️ <b>Bot Kỷ Luật 3 Việc/Ngày</b>\n\n"
            "📌 Mỗi sáng 9h → gửi 3 việc\n"
            "📌 Mỗi tối 21h → review kết quả\n"
            "📌 0h → chốt ngày, tính streak\n"
            "📌 CN 20h → báo cáo tuần\n\n"
            "Lệnh:\n"
            "  /status — Xem 3 việc hôm nay\n"
            "  /streak — Xem streak\n"
            "  /done 1 — Đánh dấu xong việc 1\n"
            "  /add — Nhập 3 việc thủ công\n\n"
            "🔥 <i>Kỷ luật tạo tự do!</i>",
            chat_id
        )
        return jsonify({"ok": True}), 200

    if cmd == "/status":
        tasks = get_today_tasks()
        if not tasks:
            tg_send("📋 Hôm nay chưa có việc nào.\nGửi /add để nhập 3 việc!", chat_id)
        else:
            done_count = sum(1 for t in tasks if get_status(t) == S_DONE)
            streak = calculate_current_streak()
            tg_send(
                f"📋 <b>3 việc hôm nay</b>\n\n"
                + format_task_list(tasks)
                + f"\n\n📊 {done_count}/3 | 🔥 Streak: {streak}",
                chat_id
            )
        return jsonify({"ok": True}), 200

    if cmd == "/streak":
        streak = calculate_current_streak()
        # Check hôm nay nếu đã xong hết thì +1
        today_tasks = get_today_tasks()
        today_done = len(today_tasks) >= 3 and all(get_status(t) == S_DONE for t in today_tasks)
        display_streak = streak + 1 if today_done else streak

        if display_streak >= 7:
            emoji = "🏆"
        elif display_streak >= 3:
            emoji = "🔥"
        elif display_streak >= 1:
            emoji = "⭐"
        else:
            emoji = "💤"

        tg_send(
            f"{emoji} <b>STREAK: {display_streak} ngày</b>\n\n"
            + ("Giữ lửa nha Sếp! 💪" if display_streak > 0 else "Bắt đầu lại từ hôm nay! 🚀"),
            chat_id
        )
        return jsonify({"ok": True}), 200

    if cmd.startswith("/done"):
        parts = cmd.split()
        if len(parts) < 2 or not parts[1].isdigit():
            tg_send("⚠️ Dùng: /done 1 hoặc /done 2 hoặc /done 3", chat_id)
            return jsonify({"ok": True}), 200

        num = int(parts[1])
        if num < 1 or num > 3:
            tg_send("⚠️ Chỉ có việc 1, 2, 3 thôi Sếp!", chat_id)
            return jsonify({"ok": True}), 200

        tasks = get_today_tasks()
        target = None
        for t in tasks:
            if get_order(t) == num:
                target = t
                break

        if not target:
            tg_send(f"⚠️ Không tìm thấy việc số {num} hôm nay.", chat_id)
            return jsonify({"ok": True}), 200

        if get_status(target) == S_DONE:
            tg_send(f"✅ Việc {num} đã xong rồi!", chat_id)
            return jsonify({"ok": True}), 200

        notion_update(target["id"], {P_STATUS: {"select": {"name": S_DONE}}})
        title = get_title(target)

        # Check 3/3 chưa
        done_count = sum(1 for t in tasks if get_status(t) == S_DONE) + 1
        if done_count >= 3:
            streak = calculate_current_streak()
            tg_send(
                f"✅ <b>Xong: {title}</b>\n\n"
                f"🎉 PERFECT DAY! 3/3 hoàn thành!\n"
                f"🔥 Streak: {streak + 1} ngày!",
                chat_id
            )
        else:
            tg_send(
                f"✅ <b>Xong: {title}</b>\n"
                f"📊 Tiến độ: {done_count}/3",
                chat_id
            )
        return jsonify({"ok": True}), 200

    if cmd == "/add":
        existing = get_today_tasks()
        if len(existing) >= 3:
            tg_send(
                "⚠️ Hôm nay đã có 3 việc rồi!\n\n"
                + format_task_list(existing),
                chat_id
            )
            return jsonify({"ok": True}), 200

        WAITING_TASKS[chat_id] = True
        tg_send("📝 Gửi 3 dòng, mỗi dòng 1 việc 👇", chat_id)
        return jsonify({"ok": True}), 200

    # ── Đang chờ nhập 3 việc ──
    if WAITING_TASKS.get(chat_id):
        return handle_task_input(text, chat_id)

    # ── Không nhận diện ──
    tg_send(
        "❓ Em không hiểu. Dùng:\n"
        "  /status — Xem 3 việc hôm nay\n"
        "  /done 1 — Đánh dấu xong\n"
        "  /streak — Xem streak\n"
        "  /add — Nhập 3 việc",
        chat_id
    )
    return jsonify({"ok": True}), 200


def handle_task_input(text, chat_id):
    """Xử lý khi Sếp gửi 3 việc"""
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]

    if len(lines) < 3:
        tg_send(
            f"⚠️ Sếp mới gửi {len(lines)} dòng, cần đủ <b>3 dòng</b>!\n"
            "Gửi lại 3 việc nha 👇",
            chat_id
        )
        return jsonify({"ok": True}), 200

    # Lấy 3 dòng đầu
    tasks = lines[:3]
    date = today_str()
    streak = calculate_current_streak()

    success = 0
    for i, task_name in enumerate(tasks, 1):
        if notion_create(task_name, date, i, streak):
            success += 1

    WAITING_TASKS.pop(chat_id, None)

    if success == 3:
        tg_send(
            "✅ <b>Đã ghi nhận 3 việc hôm nay!</b>\n\n"
            f"  1️⃣ {tasks[0]}\n"
            f"  2️⃣ {tasks[1]}\n"
            f"  3️⃣ {tasks[2]}\n\n"
            "🔥 Chiến đi Sếp! 💪",
            chat_id
        )
    else:
        tg_send(f"⚠️ Tạo được {success}/3 task. Kiểm tra lại Notion DB.", chat_id)

    return jsonify({"ok": True}), 200


def handle_callback(callback):
    """Xử lý khi Sếp bấm nút inline"""
    cb_data = callback.get("data", "")
    cb_id = callback.get("id")
    chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))

    # Answer callback
    if TELEGRAM_TOKEN and cb_id:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
                json={"callback_query_id": cb_id},
                timeout=5,
            )
        except:
            pass

    if not cb_data.startswith("done:"):
        return jsonify({"ok": True}), 200

    page_id = cb_data.replace("done:", "")

    # Update status = Xong
    notion_update(page_id, {P_STATUS: {"select": {"name": S_DONE}}})

    # Lấy title
    try:
        r = requests.get(
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=NOTION_HEADERS, timeout=10
        )
        page = r.json()
        title = get_title(page)
    except:
        title = "—"

    # Check tiến độ
    tasks = get_today_tasks()
    done_count = sum(1 for t in tasks if get_status(t) == S_DONE)

    if done_count >= 3:
        streak = calculate_current_streak()
        tg_send(
            f"✅ <b>Xong: {title}</b>\n\n"
            f"🎉 PERFECT DAY! 3/3!\n"
            f"🔥 Streak: {streak + 1} ngày!",
            chat_id
        )
    else:
        remaining = 3 - done_count
        tg_send(
            f"✅ <b>Xong: {title}</b>\n"
            f"📊 {done_count}/3 — còn {remaining} việc!",
            chat_id
        )

    return jsonify({"ok": True}), 200


# ═══════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health():
    return "OK", 200


@app.route("/wake", methods=["GET"])
def wake():
    return "ok", 200


# ═══════════════════════════════════════════════════════════════
# SCHEDULER
# ═══════════════════════════════════════════════════════════════

def start_scheduler():
    sched = BackgroundScheduler(timezone="Asia/Ho_Chi_Minh")
    sched.add_job(job_morning, "cron", hour=9, minute=0, id="morning")
    sched.add_job(job_evening, "cron", hour=21, minute=0, id="evening")
    sched.add_job(job_midnight, "cron", hour=0, minute=5, id="midnight")
    sched.add_job(job_weekly, "cron", day_of_week="sun", hour=20, minute=0, id="weekly")
    sched.start()
    print(f"[SCHEDULER] Started — 9h/21h/0h05/CN20h ({TZ})")


def set_webhook():
    if TELEGRAM_TOKEN and WEBHOOK_URL:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
        try:
            r = requests.post(url, json={"url": WEBHOOK_URL}, timeout=10)
            print(f"[WEBHOOK] {r.status_code} — {r.json()}")
        except Exception as e:
            print(f"[WEBHOOK ERR] {e}")


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 50)
    print("🏋️  KỶ LUẬT 3 VIỆC/NGÀY")
    print("=" * 50)
    print(f"DB: {NOTION_DB_ID[:12]}...")
    print(f"Chat: {CHAT_ID}")

    set_webhook()
    start_scheduler()

    port = int(os.getenv("PORT", 5000))
    print(f"[SERVER] Running on port {port}")
    app.run(host="0.0.0.0", port=port, threaded=True)
