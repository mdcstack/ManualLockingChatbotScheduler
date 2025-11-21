from pymongo import MongoClient
from bson.objectid import ObjectId
from datetime import datetime, time, timedelta, timezone


class DBService:
    """
    Handles all interactions with the MongoDB collections (users, tasks, etc.).
    All public methods use the user's string-based ObjectId.
    """

    def __init__(self, db_connection):
        self.db = db_connection
        self.users_collection = self.db["users"]

    # --- INTERNAL HELPER ---
    def _parse_deadline_to_aware(self, item):
        """Helper to convert task/test deadline strings to timezone-aware datetime objects."""
        deadline_str = item.get("deadline", item.get("date"))
        if not deadline_str: return None
        if 'T' not in deadline_str:
            deadline_str += "T23:59:59"

        try:
            dt = datetime.fromisoformat(deadline_str)
            # If naive, assume it's in UTC (standard for server-side persistence)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            return None

    # --- END INTERNAL HELPER ---

    # --- TOKEN-SAVING READ OPERATION ---

    def get_active_context_data(self, user_id, now_dt):
        """
        Fetches user data and prunes tasks/tests to include only items relevant
        to the next 30 days or those marked as 'top' priority.
        """
        user_data = self.get_user_data(user_id)
        if not user_data:
            return None

        cutoff_dt = now_dt + timedelta(days=30)

        active_tasks = []
        for task in user_data.get("tasks", []):
            deadline = self._parse_deadline_to_aware(task)
            priority = task.get("priority")

            if deadline and (deadline < cutoff_dt or priority == "top"):
                active_tasks.append(task)

        active_tests = []
        for test in user_data.get("tests", []):
            deadline = self._parse_deadline_to_aware(test)
            priority = test.get("priority")

            if deadline and (deadline < cutoff_dt or priority == "top"):
                active_tests.append(test)

        pruned_context = {
            "schedule": user_data.get("schedule", []),
            "tasks": active_tasks,
            "tests": active_tests,
            "preferences": user_data.get("preferences", {}),
            "study_windows": user_data.get("study_windows", [])
        }

        return pruned_context

    # --- END NEW READ OPERATION ---

    # --- NEW CRUD OPERATIONS ---
    def set_onboarding_complete(self, user_id, status):
        """Sets the onboarding status flag for the user."""
        result = self.users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"onboarding_complete": status}}
        )
        return result.modified_count > 0

    # --- ORIGINAL READ OPERATIONS (Kept for compatibility) ---
    def get_user_data(self, user_id):
        """Fetches all user data by ObjectId."""
        return self.users_collection.find_one({"_id": ObjectId(user_id)})

    # --- ORIGINAL CRUD OPERATIONS (Unchanged functionality) ---
    def update_user_preference(self, user_id, preferences):
        """Saves awake and sleep time preferences."""
        result = self.users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"preferences": preferences}}
        )
        return result.modified_count > 0

    def add_schedule_item(self, user_id, data_type, data):
        """Adds a class, task, or test to the user's document."""
        if data_type == "class":
            update_field = "schedule"
        elif data_type == "task":
            update_field = "tasks"
        elif data_type == "test":
            # Convert test 'date' to a full 'deadline' for consistency
            data['deadline'] = f"{data['date']}T23:59:59"
            update_field = "tests"
        else:
            raise ValueError("Invalid data_type provided.")

        result = self.users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {"$push": {update_field: data}}
        )
        return result.modified_count > 0

    def update_task_details(self, user_id, args):
        """Updates an existing task or test details."""
        current_name = args.get("current_name")
        updates = {}
        target_array = None

        if self.users_collection.find_one({"_id": ObjectId(user_id), "tasks.name": current_name}):
            target_array = "tasks"
        elif self.users_collection.find_one({"_id": ObjectId(user_id), "tests.name": current_name}):
            target_array = "tests"

        if not target_array: return 0

        if args.get("new_name"): updates[f"{target_array}.$.name"] = args["new_name"]
        if args.get("new_task_type"):
            type_field = "test_type" if target_array == "tests" else "task_type"
            updates[f"{target_array}.$.{type_field}"] = args["new_task_type"]
        if args.get("new_deadline"): updates[f"{target_array}.$.deadline"] = args["new_deadline"]
        if args.get("new_priority"): updates[f"{target_array}.$.priority"] = args["new_priority"]
        if args.get("new_duration_hours") is not None: updates[f"{target_array}.$.duration_hours"] = args[
            "new_duration_hours"]

        if not updates: return -1

        result = self.users_collection.update_one(
            {"_id": ObjectId(user_id), f"{target_array}.name": current_name},
            {"$set": updates}
        )

        if args.get("new_name") and result.modified_count > 0:
            self.users_collection.update_one(
                {"_id": ObjectId(user_id)},
                {"$set": {"generated_plan.$[elem].task": f"Work on {args['new_name']}"}},
                array_filters=[{"elem.task": {"$regex": current_name, "$options": "i"}}]
            )
        return result.modified_count

    def update_class_schedule(self, user_id, args):
        """Updates the day, start time, or end time of an existing class."""
        subject = args.get("subject")
        updates = {}
        if "new_day" in args: updates["schedule.$.day"] = args["new_day"]
        if "new_start_time" in args: updates["schedule.$.start_time"] = args["new_start_time"]
        if "new_end_time" in args: updates["schedule.$.end_time"] = args["new_end_time"]

        if not updates: return -1

        result = self.users_collection.update_one(
            {"_id": ObjectId(user_id), "schedule.subject": subject},
            {"$set": updates}
        )
        return result.modified_count

    def delete_schedule_item(self, user_id, item_name):
        """Deletes an item and related plan blocks."""
        result_class = self.users_collection.update_one({"_id": ObjectId(user_id)},
                                                        {"$pull": {"schedule": {"subject": item_name}}})
        result_task = self.users_collection.update_one({"_id": ObjectId(user_id)},
                                                       {"$pull": {"tasks": {"name": item_name}}})
        result_test = self.users_collection.update_one({"_id": ObjectId(user_id)},
                                                       {"$pull": {"tests": {"name": item_name}}})
        result_plan = self.users_collection.update_one({"_id": ObjectId(user_id)}, {
            "$pull": {"generated_plan": {"task": {"$regex": item_name, "$options": "i"}}}})

        return (result_class.modified_count > 0 or result_task.modified_count > 0 or
                result_test.modified_count > 0 or result_plan.modified_count > 0)

    def save_study_windows(self, user_id, windows):
        """Saves new study windows by PUSHING (adding) them to the array."""

        # CRITICAL FIX: Use $push instead of $set
        result = self.users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {"$push": {"study_windows": {"$each": windows}}}  # Use $each for array items
        )
        return result.modified_count > 0

    def update_generated_plan(self, user_id, new_plan):
        """Saves the result of the planning engine."""
        result = self.users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"generated_plan": new_plan}}
        )
        return result.modified_count > 0

    def auto_cleanup_past_items(self, user_id, now_dt):
        """Removes past tasks, tests, and plan blocks based on client time."""
        now_iso = now_dt.isoformat()
        today_date_str = now_dt.strftime("%Y-%m-%d")

        self.users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {
                "$pull": {
                    "tasks": {"deadline": {"$lt": now_iso}},
                    "tests": {"date": {"$lt": today_date_str}},
                    "generated_plan": {"date": {"$lt": today_date_str}}
                }
            }
        )