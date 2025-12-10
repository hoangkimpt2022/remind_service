def job_daily():
    global LAST_TASKS  # Khai báo global ở đầu hàm để tránh SyntaxError
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

    LAST_TASKS = [p.get("id") for p in weekly_tasks]  # Gán sau global
