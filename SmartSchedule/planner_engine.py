from datetime import datetime, timedelta, time, timezone

# --- CONSTANTS ---
PH_TZ = timezone(timedelta(hours=8))  # >>> Philippines timezone <<<

DEFAULT_PRIORITY_MAP = {
    "top": 0, "high": 1, "medium": 2, "low": 3,
    "exam": 1, "project": 2, "quiz": 3, "assignment": 4, "seatwork": 5
}
# Ideal session size used for Context-Aware Sizing
SESSION_IDEAL_DURATION_MAP = {
    "exam": 3,
    "project": 5,
    "quiz": 1,
    "assignment": 1,
    "seatwork": 1,
    "default": 1
}

DAY_OF_WEEK_MAP = {
    0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday",
    4: "Friday", 5: "Saturday", 6: "Sunday"
}

INFINITE_BLOCKS = 999


# --- HELPER FUNCTIONS (Unchanged) ---
def _time_to_minutes(time_str):
    """Converts HH:MM string to minutes since midnight."""
    try:
        t = time.fromisoformat(time_str)
        return t.hour * 60 + t.minute
    except ValueError:
        return 0


def _format_time_12hr(time_str):
    """Converts an 'HH:MM' string to 'H:MM AM/PM'."""
    if not time_str or ':' not in time_str:
        return time_str
    try:
        t = time.fromisoformat(time_str)
        return t.strftime("%I:%M %p").lstrip('0')
    except ValueError:
        return time_str


# --- MASTER PLANNER CLASS ---
class PlannerEngine:
    """
    Manages all scheduling and plan generation algorithms.
    """

    def __init__(self, db_service):
        self.db_service = db_service

    # --- CORE PLANNING ALGORITHM (SIMPLIFIED) ---

    def run_planner_engine(self, user_id, args, now_dt):
        """
        Runs the simple schedule generator using PH timezone.
        """
        now_dt = now_dt.astimezone(PH_TZ)

        user_data = self.db_service.get_user_data(user_id)

        work_items = self._build_work_queue(user_data, now_dt)
        if not work_items:
            return {
                "status": "success",
                "message": "Planner ran, but you have no upcoming tasks or tests to plan for."
            }

        new_plan = self._run_simple_scheduler(user_data, work_items, now_dt)

        self.db_service.update_generated_plan(user_id, new_plan)

        return {"status": "success",
                "message": "I've regenerated your study plan up to your deadlines (PH Time)."}

    # --- SUB-FUNCTION IMPLEMENTATIONS ---

    def _build_work_queue(self, user_data, now_dt):
        """Creates and sorts the list of pending tasks and tests."""
        work_items = []
        all_items = user_data.get("tasks", []) + user_data.get("tests", [])
        for item in all_items:
            try:
                deadline_str = item.get("deadline", item.get("date"))
                if 'T' not in deadline_str:
                    deadline_str += "T23:59:59"

                # Parse and force to PH timezone
                deadline = datetime.fromisoformat(deadline_str)
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=PH_TZ)
                else:
                    deadline = deadline.astimezone(PH_TZ)

                if deadline < now_dt: continue

                priority_str = item.get("priority", item.get("task_type", item.get("test_type", "low")))
                priority_score = DEFAULT_PRIORITY_MAP.get(priority_str, 99)

                item_type = item.get("task_type", item.get("test_type"))

                duration_blocks = item.get("duration_hours", INFINITE_BLOCKS)

                work_items.append({
                    "name": item.get("name"),
                    "deadline": deadline,
                    "priority": priority_score,
                    "blocks_needed": duration_blocks,
                    "blocks_allocated": 0,
                    "item_type": item_type
                })
            except Exception as e:
                print(f"Skipping item due to parse error: {item.get('name')}, {e}")

        work_items.sort(key=lambda x: (x["priority"], x["deadline"]))
        return work_items

    def _run_simple_scheduler(self, user_data, work_items, now_dt):
        """
        Iterates over days until the day BEFORE the furthest deadline and fills available study windows
        with the highest priority task, respecting the session size rule.
        """
        new_plan = []
        study_windows = user_data.get("study_windows", [])

        # 1. Determine the absolute stop date
        furthest_deadline = now_dt + timedelta(days=7)
        if work_items:
            latest = work_items[-1]["deadline"]
            if latest > furthest_deadline:
                furthest_deadline = latest

        # CRITICAL FIX: Set the loop stop point to MIDNIGHT of the deadline day.
        # This ensures the loop runs for the day *before* the deadline but stops when the deadline day starts.
        stop_date = datetime.combine(furthest_deadline.date(), time(0), tzinfo=PH_TZ)

        start_date = now_dt.date()
        current_day = start_date

        # 2. Main scheduling loop
        while datetime.combine(current_day, time(0), tzinfo=PH_TZ) < stop_date:

            day_str = current_day.strftime("%Y-%m-%d")
            day_name = DAY_OF_WEEK_MAP.get(current_day.weekday())

            is_today = (current_day == start_date)

            windows_for_day = [w for w in study_windows if w.get("day") == day_name]

            # Iterate over the study windows in order
            for window in windows_for_day:
                win_start_str = window.get("start_time")
                win_end_str = window.get("end_time")
                if not win_start_str or not win_end_str: continue

                win_start_min = _time_to_minutes(win_start_str)
                win_end_min = _time_to_minutes(win_end_str)

                # Set the window start time, forced to PH_TZ
                current_time = datetime.combine(
                    current_day, time.fromisoformat(win_start_str), tzinfo=PH_TZ
                )

                # If today, adjust window start to the next full hour
                if is_today and current_time < now_dt:
                    next_hour = now_dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

                    if next_hour.hour * 60 + next_hour.minute >= win_end_min:
                        continue

                    current_time = max(current_time, next_hour)

                    if current_time.hour * 60 + current_time.minute >= win_end_min:
                        continue

                # Scheduling loop
                while (current_time.hour * 60 + current_time.minute) < win_end_min:

                    assigned_task = None

                    for item in work_items:
                        is_complete = (
                                item["blocks_needed"] != INFINITE_BLOCKS and
                                item["blocks_allocated"] >= item["blocks_needed"]
                        )

                        # Check deadline: Ensure scheduling stops BEFORE the deadline
                        # The outer loop already ensures we don't schedule ON the deadline day,
                        # but this internal check remains vital for multi-task scheduling.
                        if is_complete or current_time >= item["deadline"]:
                            continue

                        assigned_task = item
                        break

                    if not assigned_task:
                        break

                        # --- Determine ideal session size ---
                    item_type = assigned_task.get("item_type", "default")
                    ideal_session_size = SESSION_IDEAL_DURATION_MAP.get(item_type, 1)

                    remaining_minutes = win_end_min - (current_time.hour * 60 + current_time.minute)
                    remaining_hours = remaining_minutes / 60

                    allocation_hours = min(ideal_session_size, remaining_hours)

                    if assigned_task["blocks_needed"] != INFINITE_BLOCKS:
                        allocation_hours = min(allocation_hours,
                                               assigned_task["blocks_needed"] - assigned_task["blocks_allocated"])

                    allocation_hours = int(allocation_hours)
                    if allocation_hours == 0:
                        break

                    # Allocate the slot
                    slot_start = current_time
                    slot_end = slot_start + timedelta(hours=allocation_hours)

                    new_plan.append({
                        "date": day_str,
                        "start_time": slot_start.strftime("%H:%M"),
                        "end_time": slot_end.strftime("%H:%M"),
                        "task": f"Work on {assigned_task['name']}"
                    })

                    if assigned_task["blocks_needed"] != INFINITE_BLOCKS:
                        assigned_task["blocks_allocated"] += allocation_hours

                    current_time = slot_end

            current_day += timedelta(days=1)

        new_plan.sort(key=lambda x: (x["date"], x["start_time"]))
        return new_plan

    # --- TOOL IMPLEMENTATIONS (Restored for class completeness) ---
    def get_daily_plan(self, user_id):
        user_data = self.db_service.get_user_data(user_id)
        generated_plan = user_data.get("generated_plan", [])

        today_str = datetime.now(PH_TZ).strftime("%Y-%m-%d")

        todays_items = [i for i in generated_plan if i["date"] == today_str]

        if not todays_items:
            return "You have no study blocks for today."

        summary = ", ".join(
            f"{i['task']} from {_format_time_12hr(i['start_time'])}"
            f" to {_format_time_12hr(i['end_time'])}"
            for i in todays_items
        )

        return f"Your plan for today (PH Time): {summary}."

    def get_priority_list(self, user_id, args, now_dt):
        user_data = self.db_service.get_user_data(user_id)
        now_dt = now_dt.astimezone(PH_TZ)
        available_hours = args.get("hours", 1)

        work_items = self._build_work_queue(user_data, now_dt)
        if not work_items:
            return "You have no pending tasks!"

        suggested = []
        total_duration = 0

        for item in work_items:
            item_type = item.get("item_type", "default")
            session_size = SESSION_IDEAL_DURATION_MAP.get(item_type, 1)

            if (total_duration + session_size) <= available_hours:
                suggested.append(item)
                total_duration += session_size
            if total_duration == available_hours:
                break

        if not suggested:
            return (
                f"You have {available_hours} hours, but your top task "
                f"({work_items[0]['name']}) needs more time."
            )

        response = ["Here is your priority list:"]
        for i, task in enumerate(suggested):
            hour_str = "hour" if session_size == 1 else "hours"
            response.append(f"{i + 1}. Work on **{task['name']}** (est. {session_size} {hour_str})")

        response.append(f"\nTotal: **{total_duration} hours**")
        return "\n".join(response)

    def reschedule_day(self, user_id, args, now_dt):
        now_dt = now_dt.astimezone(PH_TZ)
        time_blocks = args.get("time_blocks", [])
        today_str = now_dt.strftime("%Y-%m-%d")
        daily_overrides = {today_str: time_blocks}

        planner_args = {"daily_overrides": daily_overrides, "force_auto": True}

        return self.run_planner_engine(user_id, planner_args, now_dt)