from datetime import datetime, timedelta, time, timezone
import calendar

# --- CONSTANTS ---
PH_TZ = timezone(timedelta(hours=8))  # >>> Philippines timezone <<<

DEFAULT_PRIORITY_MAP = {
    "top": 0, "high": 1, "medium": 2, "low": 3,
    "exam": 1, "project": 5, "quiz": 3, "assignment": 4, "seatwork": 5
}

# Ideal session size used for Context-Aware Sizing
# UPDATED: Now supports 0.5 (30 minute) granularity
SESSION_IDEAL_DURATION_MAP = {
    "exam": 3.0,
    "project": 2.0,  # Reduced cap to encourage breaks
    "quiz": 1.0,  # Standard quiz study
    "assignment": 1.0,
    "seatwork": 0.5,  # 30 mins for quick tasks
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

    # --- TOOL EXECUTION: SCHEDULE RECURRING BLOCKS ---

    def schedule_recurring_blocks(self, user_id, args, now_dt):
        """
        Tool called by the chatbot to save a recurring study plan.
        This handles all recurrence logic and validation.
        """
        item_name = args.get("item_name")
        days = args.get("days", [])
        start_time = args.get("start_time")
        end_time = args.get("end_time")

        user_data = self.db_service.get_user_data(user_id)
        all_items = user_data.get("tasks", []) + user_data.get("tests", [])

        target_item = next((item for item in all_items if item.get("name").lower() == item_name.lower()), None)

        if not target_item:
            return {"status": "error",
                    "message": f"Sorry, I couldn't find a task or test named '{item_name}'. You must save the task/test first."}

        # Parse the deadline string into a datetime object for internal use
        try:
            deadline_str = target_item['deadline']
            if len(deadline_str.split('T')) == 1:
                deadline_str += 'T23:59:59'
            target_item['deadline_dt'] = datetime.fromisoformat(deadline_str).replace(tzinfo=PH_TZ)
        except Exception as e:
            return {"status": "error", "message": "Internal Error: Could not parse task deadline."}

        # 1. Generate new plan entries based on recurrence and constraints
        new_plan_entries, messages = self._generate_recurring_blocks(user_data, target_item, days, start_time, end_time,
                                                                     now_dt)

        # 2. Consolidate new entries with existing plan entries
        current_plan = user_data.get("generated_plan", [])

        # CRITICAL: Remove any EXISTING plan blocks for this specific item before adding new ones
        current_plan = [p for p in current_plan if p['task'] != f"Work on {item_name}"]

        final_plan = current_plan + new_plan_entries

        # 3. Update the DB
        self.db_service.update_generated_plan(user_id, final_plan)

        message = f"Study blocks for '{item_name}' have been scheduled until the deadline."
        if messages:
            message = "Study blocks scheduled with the following notes: " + " ".join(messages)

        return {"status": "success", "message": message}

    # --- CORE RECURRENCE GENERATION LOGIC ---

    def _generate_recurring_blocks(self, user_data, target_item, days, start_time, end_time, now_dt):
        """
        Iterates from today until the task deadline, generating and validating
        blocks for the specified recurring days.
        """
        generated_blocks = []
        messages = []

        # Use the newly parsed deadline_dt (now a datetime object)
        deadline_dt = target_item["deadline_dt"]
        current_day = now_dt.date()  # Start day is the date part of client now

        # UPDATED: Granular Sizing Logic
        item_type = target_item.get("task_type", target_item.get("test_type", "default"))
        ideal_session_size = SESSION_IDEAL_DURATION_MAP.get(item_type, 1.0)  # Float default

        classes = user_data.get("schedule", [])
        target_day_indices = [DAY_MAP_TO_INDEX.get(d) for d in days if d in DAY_MAP_TO_INDEX]

        requested_start_min = _time_to_minutes(start_time)
        requested_end_min = _time_to_minutes(end_time)

        # Loop stops *before* the deadline day begins (midnight)
        stop_date = datetime.combine(deadline_dt.date(), time(0), tzinfo=PH_TZ)

        # Start iterating from today's date
        while datetime.combine(current_day, time(0), tzinfo=PH_TZ) < stop_date:

            if current_day.weekday() in target_day_indices:

                # Setup proposed block times (PH_TZ aware)
                block_start_time_naive = time.fromisoformat(start_time)
                block_end_time_naive = time.fromisoformat(end_time)

                block_start_dt = datetime.combine(current_day, block_start_time_naive, tzinfo=PH_TZ)
                block_end_dt = datetime.combine(current_day, block_end_time_naive, tzinfo=PH_TZ)

                # 1. PAST TIME CHECK (If scheduling for today)
                if current_day == now_dt.date() and block_end_dt < now_dt.astimezone(PH_TZ):
                    messages.append(
                        f"Skipping {DAY_OF_WEEK_MAP[current_day.weekday()]} block: Time slot has passed today.")
                    current_day += timedelta(days=1)
                    continue

                # 2. CLASS CONFLICT CHECK
                is_conflict, conflicting_subject = self._check_class_conflict(current_day, requested_start_min,
                                                                              requested_end_min, classes)

                if is_conflict:
                    messages.append(
                        f"Skipping {current_day.strftime('%b %d')} block: Conflict with class '{conflicting_subject}'.")
                    current_day += timedelta(days=1)
                    continue

                # 3. SESSION CAP CHECK (Updated for Floats)
                duration_hours = (block_end_dt - block_start_dt).total_seconds() / 3600.0

                # We prioritize the User's requested window, but warn if it exceeds ideal size for that type
                # (Logic adjusted: We only cap if it's significantly larger, otherwise trust user input for recurring blocks)
                allocated_hours = min(duration_hours, ideal_session_size)

                # If the user asks for 2 hours for a 'seatwork' (ideal 0.5), we cap it.
                final_end_dt = block_start_dt + timedelta(hours=allocated_hours)

                if allocated_hours < duration_hours:
                    messages.append(
                        f"Note: Block on {current_day.strftime('%b %d')} capped at {allocated_hours} hr(s) due to {item_type} limits.")

                # 4. Add to Plan
                generated_blocks.append({
                    "date": current_day.strftime("%Y-%m-%d"),
                    "start_time": block_start_dt.strftime("%H:%M"),
                    "end_time": final_end_dt.strftime("%H:%M"),
                    "task": f"Work on {target_item['name']}",
                    "completed": False  # Default to not done
                })

            current_day += timedelta(days=1)

        return generated_blocks, messages

    # --- CONFLICT CHECK HELPER (Retained) ---
    def _check_class_conflict(self, block_date_dt, block_start_min, block_end_min, classes):
        """
        Checks if the proposed study block conflicts with any user's fixed classes.
        """
        day_name = DAY_OF_WEEK_MAP.get(block_date_dt.weekday())

        for cls in classes:
            if cls.get('day') == day_name:
                class_start_min = _time_to_minutes(cls.get('start_time'))
                class_end_min = _time_to_minutes(cls.get('end_time'))

                if _check_overlap(block_start_min, block_end_min, class_start_min, class_end_min):
                    return True, cls.get('subject')  # Conflict found
        return False, None

    # --- STANDARD PLANNER FUNCTIONS ---

    def run_planner_engine(self, user_id, args, now_dt):
        """
        Runs the planner for consolidation (retained for generic save_task triggers).
        """
        now_dt = now_dt.astimezone(PH_TZ)
        user_data = self.db_service.get_user_data(user_id)

        final_plan = user_data.get("generated_plan", [])

        # Cleanup past items
        today_str = now_dt.strftime("%Y-%m-%d")
        final_plan = [p for p in final_plan if p['date'] >= today_str]
        final_plan.sort(key=lambda x: (x["date"], x["start_time"]))

        self.db_service.update_generated_plan(user_id, final_plan)

        return {"status": "success", "message": "Plan sorted and validated."}

    def _build_work_queue(self, user_data, now_dt):
        """Creates a list of pending tasks/tests for constraint checking."""
        work_items = []
        all_items = user_data.get("tasks", []) + user_data.get("tests", [])
        for item in all_items:
            try:
                deadline_str = item.get("deadline", item.get("date"))
                if 'T' not in deadline_str: deadline_str += "T23:59:59"
                deadline = datetime.fromisoformat(deadline_str).replace(tzinfo=PH_TZ)

                item_type = item.get("task_type", item.get("test_type"))

                work_items.append({
                    "name": item.get("name"),
                    "deadline": deadline,
                    "item_type": item_type
                })
            except Exception as e:
                print(f"Skipping item due to parse error: {item.get('name')}, {e}")

        return work_items

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
        return "The priority list feature is temporarily disabled."

    def reschedule_day(self, user_id, args, now_dt):
        return self.run_planner_engine(user_id, {"force_auto": True}, now_dt)