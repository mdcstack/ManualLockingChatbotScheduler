from datetime import datetime, timedelta, time, timezone
import calendar

# --- CONSTANTS ---
PH_TZ = timezone(timedelta(hours=8))  # >>> Philippines timezone <<<

DEFAULT_PRIORITY_MAP = {
    "top": 0, "high": 1, "medium": 2, "low": 3,
    "exam": 1, "project": 5, "quiz": 3, "assignment": 4, "seatwork": 5
}

# Ideal session size used for Context-Aware Sizing
SESSION_IDEAL_DURATION_MAP = {
    "exam": 3.0,
    "project": 2.0,
    "quiz": 1.0,
    "assignment": 1.0,
    "seatwork": 0.5,
    "default": 1.0
}

DAY_OF_WEEK_MAP = {
    0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday",
    4: "Friday", 5: "Saturday", 6: "Sunday"
}

# Map day name to Python's weekday index (Monday=0, Sunday=6)
DAY_MAP_TO_INDEX = {
    "Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
    "Friday": 4, "Saturday": 5, "Sunday": 6
}

INFINITE_BLOCKS = 999


# --- HELPER FUNCTIONS ---
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


def _check_overlap(start1, end1, start2, end2):
    """Checks if two time ranges (in minutes) overlap."""
    return start1 < end2 and start2 < end1


# --- MASTER PLANNER CLASS ---
class PlannerEngine:

    def __init__(self, db_service):
        self.db_service = db_service

    # =========================================================
    # === NEW: CONFLICT DETECTION LOGIC (The "Pre-Flight Check") ===
    # =========================================================
    def detect_conflicts(self, user_id, args, now_dt):
        """
        Simulates the schedule to check for overlaps BEFORE saving.
        Returns: { 'has_conflict': bool, 'details': [strings] }
        """
        user_data = self.db_service.get_user_data(user_id)

        # 1. Parse Request
        item_name = args.get("item_name")
        target_days = args.get("days", [])
        start_time_str = args.get("start_time")
        end_time_str = args.get("end_time")

        # Basic validation
        if not (target_days and start_time_str and end_time_str):
            # If missing info, we can't detect conflicts reliably.
            # Return False so the main tool handles the error naturally.
            return {"has_conflict": False, "details": []}

        # 2. Find the Deadline
        all_items = user_data.get("tasks", []) + user_data.get("tests", [])

        # Simple fuzzy match for item name
        target_item = next((i for i in all_items if i["name"].lower() in item_name.lower()), None)

        deadline_dt = None
        if target_item:
            d_str = target_item.get("deadline") or target_item.get("date")
            if d_str:
                if 'T' not in d_str: d_str += "T23:59:59"
                try:
                    deadline_dt = datetime.fromisoformat(d_str).replace(tzinfo=PH_TZ)
                except ValueError:
                    pass

        if not deadline_dt:
            # If deadline is missing/invalid, we skip conflict check
            return {"has_conflict": False, "details": []}

        # 3. Simulation Loop
        conflicts = []

        # Normalize Times to Minutes for comparison
        req_start_min = _time_to_minutes(start_time_str)
        req_end_min = _time_to_minutes(end_time_str)

        # Start looking from Tomorrow
        curr_date = now_dt + timedelta(days=1)

        # Safety cap: don't loop forever if deadline is years away
        while curr_date <= deadline_dt and (curr_date - now_dt).days < 365:
            day_name = DAY_OF_WEEK_MAP[curr_date.weekday()]

            if day_name in target_days:
                date_str = curr_date.strftime("%Y-%m-%d")

                # A. Check Fixed Classes
                is_class_conflict, cls_subject = self._check_class_conflict(
                    curr_date, req_start_min, req_end_min, user_data.get("schedule", [])
                )
                if is_class_conflict:
                    conflicts.append(f"{day_name} ({date_str}) overlaps with Class: {cls_subject}")

                # B. Check Existing Study Blocks
                # We reuse logic similar to class check but for the generated_plan array
                overlap_study = self._check_study_overlap(
                    user_data.get("generated_plan", []), date_str, req_start_min, req_end_min, item_name
                )
                if overlap_study:
                    conflicts.append(f"{day_name} ({date_str}) overlaps with Study Session: {overlap_study}")

            curr_date += timedelta(days=1)

        if len(conflicts) > 0:
            return {"has_conflict": True, "details": conflicts}

        return {"has_conflict": False, "details": []}

    def _check_study_overlap(self, plan_list, date_str, req_start, req_end, current_item_name):
        """Helper to check overlap against existing generated plans."""
        for block in plan_list:
            if block["date"] == date_str:
                # Don't conflict with itself (if re-scheduling the same task)
                if current_item_name and current_item_name.lower() in block["task"].lower():
                    continue

                b_start = _time_to_minutes(block["start_time"])
                b_end = _time_to_minutes(block["end_time"])

                if _check_overlap(req_start, req_end, b_start, b_end):
                    return block["task"]
        return None

    # =========================================================
    # === EXISTING SCHEDULER LOGIC (Updated to support Force Save) ===
    # =========================================================

    def schedule_recurring_blocks(self, user_id, args, now_dt):
        """
        Tool called by the chatbot to save a recurring study plan.
        This handles all recurrence logic and validation.
        """
        item_name = args.get("item_name")
        days = args.get("days", [])
        start_time = args.get("start_time")
        end_time = args.get("end_time")

        # NEW: Check if this is a "Force Save" (from the Modal)
        is_force_save = args.get("force_save", False)

        user_data = self.db_service.get_user_data(user_id)
        all_items = user_data.get("tasks", []) + user_data.get("tests", [])

        target_item = next((item for item in all_items if item.get("name").lower() in item_name.lower()), None)

        if not target_item:
            return {"status": "error",
                    "message": f"Sorry, I couldn't find a task or test named '{item_name}'. You must save the task/test first."}

        try:
            deadline_str = target_item.get('deadline') or target_item.get('date')
            if 'T' not in deadline_str:
                deadline_str += 'T23:59:59'
            target_item['deadline_dt'] = datetime.fromisoformat(deadline_str).replace(tzinfo=PH_TZ)
        except Exception as e:
            return {"status": "error", "message": "Internal Error: Could not parse task deadline."}

        # 1. Generate new plan entries
        new_plan_entries, messages = self._generate_recurring_blocks(
            user_data, target_item, days, start_time, end_time, now_dt, is_force_save
        )

        # 2. Consolidate new entries
        current_plan = user_data.get("generated_plan", [])

        # Remove OLD blocks for this specific item (clean slate for this item)
        current_plan = [p for p in current_plan if item_name.lower() not in p['task'].lower()]

        final_plan = current_plan + new_plan_entries

        # 3. Update the DB
        self.db_service.update_generated_plan(user_id, final_plan)

        message = f"Study blocks for '{item_name}' have been scheduled."
        if messages:
            # If force save, we might suppress some warnings, or show them as notes
            message += " (Notes: " + "; ".join(messages) + ")"

        return {"status": "success", "message": message}

    # --- CORE RECURRENCE GENERATION LOGIC ---

    def _generate_recurring_blocks(self, user_data, target_item, days, start_time, end_time, now_dt,
                                   is_force_save=False):
        """
        Iterates from today until the task deadline, generating blocks.
        """
        generated_blocks = []
        messages = []

        deadline_dt = target_item["deadline_dt"]
        current_day = now_dt.date()

        item_type = target_item.get("task_type", target_item.get("test_type", "default"))
        ideal_session_size = SESSION_IDEAL_DURATION_MAP.get(item_type, 1.0)

        classes = user_data.get("schedule", [])
        target_day_indices = [DAY_MAP_TO_INDEX.get(d) for d in days if d in DAY_MAP_TO_INDEX]

        requested_start_min = _time_to_minutes(start_time)
        requested_end_min = _time_to_minutes(end_time)

        stop_date = datetime.combine(deadline_dt.date(), time(0), tzinfo=PH_TZ)

        while datetime.combine(current_day, time(0), tzinfo=PH_TZ) <= stop_date:

            if current_day.weekday() in target_day_indices:

                # Skip today if time has passed
                block_start_time_naive = time.fromisoformat(start_time)
                block_end_time_naive = time.fromisoformat(end_time)
                block_start_dt = datetime.combine(current_day, block_start_time_naive, tzinfo=PH_TZ)
                block_end_dt = datetime.combine(current_day, block_end_time_naive, tzinfo=PH_TZ)

                if current_day == now_dt.date() and block_end_dt < now_dt.astimezone(PH_TZ):
                    current_day += timedelta(days=1)
                    continue

                # --- CLASS CONFLICT CHECK ---
                # Logic: If it is Force Save, we SKIP the check (or ignore the result)
                # Logic: If Normal Save, we check.

                is_conflict = False
                conflicting_subject = None

                if not is_force_save:
                    is_conflict, conflicting_subject = self._check_class_conflict(current_day, requested_start_min,
                                                                                  requested_end_min, classes)

                if is_conflict:
                    messages.append(f"Skipping {current_day.strftime('%b %d')}: Conflict with '{conflicting_subject}'.")
                    current_day += timedelta(days=1)
                    continue

                # --- DURATION CAP ---
                duration_hours = (block_end_dt - block_start_dt).total_seconds() / 3600.0
                allocated_hours = min(duration_hours, ideal_session_size)
                final_end_dt = block_start_dt + timedelta(hours=allocated_hours)

                # Add to Plan
                generated_blocks.append({
                    "date": current_day.strftime("%Y-%m-%d"),
                    "start_time": block_start_dt.strftime("%H:%M"),
                    "end_time": final_end_dt.strftime("%H:%M"),
                    "task": f"Work on {target_item['name']}",
                    "completed": False
                })

            current_day += timedelta(days=1)

        return generated_blocks, messages

    # --- CONFLICT CHECK HELPER ---
    def _check_class_conflict(self, block_date_dt, block_start_min, block_end_min, classes):
        day_name = DAY_OF_WEEK_MAP.get(block_date_dt.weekday())

        for cls in classes:
            if cls.get('day') == day_name:
                class_start_min = _time_to_minutes(cls.get('start_time'))
                class_end_min = _time_to_minutes(cls.get('end_time'))

                if _check_overlap(block_start_min, block_end_min, class_start_min, class_end_min):
                    return True, cls.get('subject')
        return False, None

    # --- STANDARD PLANNER FUNCTIONS ---

    def run_planner_engine(self, user_id, args, now_dt):
        now_dt = now_dt.astimezone(PH_TZ)
        user_data = self.db_service.get_user_data(user_id)

        final_plan = user_data.get("generated_plan", [])

        # Cleanup past items
        today_str = now_dt.strftime("%Y-%m-%d")
        final_plan = [p for p in final_plan if p['date'] >= today_str]
        final_plan.sort(key=lambda x: (x["date"], x["start_time"]))

        self.db_service.update_generated_plan(user_id, final_plan)

        return {"status": "success", "message": "Plan sorted and validated."}