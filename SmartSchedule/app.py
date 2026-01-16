from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from pymongo import MongoClient
from dotenv import load_dotenv, find_dotenv
from flask_bcrypt import Bcrypt
from openai import OpenAI
from bson.objectid import ObjectId
import os
import json
from datetime import datetime, timedelta
from db_service import DBService
from planner_engine import PlannerEngine
from fpdf import FPDF
from flask import send_file
import io

# Load .env file
load_dotenv(find_dotenv(), override=True)

app = Flask(__name__)

# Load environment variables
MONGO_URI = os.getenv("MONGO_URI")
SECRET_KEY = os.getenv("SECRET_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Initialize extensions and configuration
bcrypt = Bcrypt(app)
app.secret_key = SECRET_KEY

# Connect to MongoDB
client = MongoClient(MONGO_URI)
db = client["SmartSchedule"]
users_collection = db["users"]

# Initialize DevPro's Service Layers
db_service = DBService(db)
planner_engine = PlannerEngine(db_service)

# Initialize OpenAI client
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# === NEW SYSTEM PROMPT: INTAKE SPECIALIST ===

SYSTEM_PROMPT = """
You are an 'Intake Specialist' for a study scheduler. Your ONLY goal is to gather a list of tasks, tests, and recurring study preferences from the user.

**PRE-CONFIRMATION CHECK:**
1.  **Date Sanity:** Before summarizing, check if the date exists (e.g., February 29, 2026, does not exist). If it's invalid, point it out immediately.
2.  **Subject Legitimacy:** Ensure the subject sounds like a real academic task (e.g., "Computer Hardware" is good; "asdfghj" is not).

**CORE DIRECTIVES:**
1.  **INTAKE PHASE:** Gather the specific details (Subject, Task Type, Deadline, and Preferred Study Days/Times).
2.  **CONFIRMATION SUMMARY (CRITICAL):** Before calling any save tools, you MUST present a summary to the user.
    * Format: "I'll set up [Task Name] due [Date]. We'll schedule study blocks on [Days] from [Start Time] to [End Time]. Does that look right?"
    * Wait for the user to say "Yes" or confirm.
3.  **EXECUTE ON CONFIRMATION:** Only call `save_task`, `save_test`, or `schedule_recurring_blocks` AFTER the user confirms the summary.
4.  **FINALIZE:** Once the user indicates they are finished adding ALL items (e.g., "That's all", "I'm done"), call the `finalize_setup` tool.
5.  **CONFLICT BLINDNESS (IMPORTANT):** DO NOT check for scheduling conflicts yourself. You do not have the full calculation engine. ALWAYS call the `schedule_recurring_blocks` tool even if you think the time overlaps with a class. The tool will handle the conflict detection.
6.  **REJECTION:** If the user asks for non-scheduling advice, politely refuse.
7.  **TIME ANCHOR:** The current date and time is provided in the system message.
"""

# === TOOL DEFINITIONS ===
tools = [
    {
        "type": "function",
        "function": {
            "name": "save_preference",
            "description": "Saves the user's awake and sleep time preferences.",
            "parameters": {
                "type": "object",
                "properties": {
                    "awake_time": {"type": "string", "description": "The user's wake-up time in HH:MM format."},
                    "sleep_time": {"type": "string", "description": "The user's sleep time in HH:MM format."},
                },
                "required": ["awake_time", "sleep_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_class",
            "description": "Saves a new class to the user's schedule.",
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "day": {"type": "string"},
                    "start_time": {"type": "string", "description": "Start time in HH:MM format"},
                    "end_time": {"type": "string", "description": "End time in HH:MM format"},
                },
                "required": ["subject", "day", "start_time", "end_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_task",
            "description": "Saves a new task, assignment, or project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "task_type": {"type": "string", "enum": ["assignment", "project", "seatwork"]},
                    "deadline": {"type": "string", "description": "The deadline in YYYY-MM-DDTHH:MM:SS format"},
                    "priority": {"type": "string", "enum": ["low", "medium", "high"],
                                 "description": "Optional: User's priority for this task."},
                },
                "required": ["name", "task_type", "deadline"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_test",
            "description": "Saves a new quiz or exam.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "test_type": {"type": "string", "enum": ["quiz", "exam"]},
                    "date": {"type": "string", "description": "The date of the test in YYYY-MM-DD format"},
                    "priority": {"type": "string", "enum": ["low", "medium", "high"],
                                 "description": "Optional: User's priority for studying."},
                },
                "required": ["name", "test_type", "date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_task_details",
            "description": "Updates an existing task or test. Can change its name, type, deadline, or priority.",
            "parameters": {
                "type": "object",
                "properties": {
                    "current_name": {"type": "string", "description": "The exact current name of the task or test."},
                    "new_name": {"type": "string", "description": "The new name (optional)."},
                    "new_task_type": {"type": "string", "enum": ["assignment", "project", "seatwork", "quiz", "exam"],
                                      "description": "The new type (optional)."},
                    "new_deadline": {"type": "string",
                                     "description": "The new deadline YYYY-MM-DDTHH:MM:SS (optional)."},
                    "new_priority": {"type": "string", "enum": ["top", "high", "medium", "low"],
                                     "description": "Optional: The new priority."},
                },
                "required": ["current_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_class_schedule",
            "description": "Updates the day, start time, or end time of an *existing* class, identified by its subject name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string",
                                "description": "The subject name of the class to update (e.g., 'Math')."},
                    "new_day": {"type": "string", "description": "The new day for the class (e.g., 'Monday').",
                                "optional": True},
                    "new_start_time": {"type": "string", "description": "The new start time in HH:MM format.",
                                       "optional": True},
                    "new_end_time": {"type": "string", "description": "The new end time in HH:MM format.",
                                     "optional": True}
                },
                "required": ["subject"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_schedule_item",
            "description": "Deletes an *existing* class, task, or test from the user's schedule by its name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_name": {"type": "string",
                                  "description": "The name or subject of the item to delete (e.g., 'Math', 'History Essay')."}
                },
                "required": ["item_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_recurring_blocks",
            "description": "Saves a set of recurring study blocks for a specific task until its deadline.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_name": {"type": "string", "description": "The exact name of the task or test."},
                    "days": {
                        "type": "array",
                        "items": {"type": "string",
                                  "enum": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
                                           "Sunday"]},
                        "description": "The days of the week for the recurring block."
                    },
                    "start_time": {"type": "string", "description": "Start time in HH:MM format."},
                    "end_time": {"type": "string", "description": "End time in HH:MM format."},
                },
                "required": ["item_name", "days", "start_time", "end_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finalize_setup",
            "description": "Call this ONLY when the user confirms they have finished adding all their tasks and data. This triggers the final schedule generation and locks the chat.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_planner_engine",
            "description": "Runs the full schedule validator and consolidation engine.",
            "parameters": {"type": "object", "properties": {}}
        }
    }
]


# ----------------- TOOL EXECUTION MAPPING -----------------

# --- NEW HELPER: VALIDATE DATE BEFORE SAVING ---
def validate_and_save(uid, category, args):
    date_str = args.get('deadline') or args.get('date')
    if date_str:
        clean_date = date_str.split('T')[0]
        try:
            # This checks if the date actually exists in the calendar
            datetime.strptime(clean_date, "%Y-%m-%d")
        except ValueError:
            # Return a clear error message that the AI can read back to the user
            return {"error": f"The date {clean_date} is invalid (e.g., February 29 is only for leap years)."}

    # Logic for "Legitimate Subjects"
    subject = args.get('name', '')
    # You can add a list of forbidden keywords or patterns here
    if len(subject) < 2:
        return {"error": "The subject name is too short. Please provide a descriptive name."}

    return db_service.add_schedule_item(uid, category, args)


# --- NEW HELPER: CONFLICT DETECTION WRAPPER ---
def handle_scheduling_wrapper(uid, args, now_dt):
    """
    Intercepts the AI's request to schedule blocks.
    1. Runs a simulation to detect conflicts.
    2. If conflict -> Returns a specialized dictionary to trigger the UI Modal.
    3. If clean -> Calls the actual scheduler.
    """
    # 1. Run the Pre-Flight Check
    # Note: This relies on planner_engine having the detect_conflicts method
    conflict_check = planner_engine.detect_conflicts(uid, args, now_dt)

    if conflict_check["has_conflict"]:
        # STOP! Do not save. Return instructions for the Frontend.
        return {
            "status": "conflict_detected",
            "action": "show_conflict_modal",
            "message": "I detected a conflict with existing events.",
            "proposed_data": args,  # Pass back the original AI suggestion
            "conflict_details": conflict_check["details"]
        }

    # 2. No Conflict? Proceed to Save.
    return planner_engine.schedule_recurring_blocks(uid, args, now_dt)


def map_db_update_response(func_name, result, args):
    """Generates a user-friendly response message for DB operations."""

    # --- Check for Validation Error ---
    if isinstance(result, dict) and "error" in result:
        return f"Error: {result['error']}"
    # ----------------------------------

    if func_name == "update_task_details":
        if result == 0:
            return f"Sorry, I couldn't find an item named '{args['current_name']}' to update."
        elif result == -1:
            return "You didn't tell me what to update (name, type, deadline, priority, or duration)!"
        return f"OK, I've updated the details for '{args.get('new_name') or args.get('current_name')}'."
    elif func_name == "update_class_schedule":
        if result == 0:
            return f"Sorry, I couldn't find a class with the subject '{args['subject']}' to update."
        elif result == -1:
            return "Sorry, you need to provide what you want to change (the day, start time, or end time)."
        return f"OK, I've updated your '{args['subject']}' class."
    elif func_name == "delete_schedule_item":
        if not result:
            return f"Sorry, I couldn't find an item named '{args.get('item_name')}' to delete."
        return f"OK, I've deleted '{args.get('item_name')}' and any related schedule blocks."
    elif func_name == "save_preference":
        return f"Got it! I've saved your awake time as {args['awake_time']} and sleep time as {args['sleep_time']}."
    elif func_name.startswith("save_"):
        data_type = func_name.split('_')[1]
        return f"OK, I've added the new {data_type} to your schedule."

    return "Operation successful."


function_map = {
    "save_preference": lambda uid, args: db_service.update_user_preference(uid, args),
    "save_class": lambda uid, args: db_service.add_schedule_item(uid, "class", args),

    # UPDATED: Use validate_and_save wrapper
    "save_task": lambda uid, args: validate_and_save(uid, "task", args),
    "save_test": lambda uid, args: validate_and_save(uid, "test", args),

    "update_task_details": lambda uid, args: db_service.update_task_details(uid, args),
    "update_class_schedule": lambda uid, args: db_service.update_class_schedule(uid, args),
    "delete_schedule_item": lambda uid, args: db_service.delete_schedule_item(uid, args.get("item_name")),

    # UPDATED: Point to the Wrapper instead of the Engine directly
    "schedule_recurring_blocks": lambda uid, args, now_dt: handle_scheduling_wrapper(uid, args, now_dt),

    "finalize_setup": lambda uid, args, now_dt: db_service.mark_setup_complete(uid),
    "run_planner_engine": lambda uid, args, now_dt: planner_engine.run_planner_engine(uid, args, now_dt),
}


# ---------------------------------------------------------------

# ---------- AUTH ROUTES (Updated User Initialization) ----------
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        if users_collection.find_one({"username": username}):
            return "Username already exists!"
        hashed_pw = bcrypt.generate_password_hash(password).decode("utf-8")

        new_user = users_collection.insert_one({
            "username": username, "password": hashed_pw,
            "schedule": [], "tasks": [], "tests": [],
            "preferences": {"awake_time": "07:00", "sleep_time": "23:00"},
            "chat_history": [],
            "generated_plan": [],
            # Removed onboarding_complete
            "setup_complete": False  # Default to False
        })
        return redirect(url_for("login"))
    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        user = users_collection.find_one({"username": username})
        if user and bcrypt.check_password_hash(user["password"], password):
            session["username"] = username
            session["user_id"] = str(user["_id"])
            users_collection.update_one(
                {"username": username},
                {"$set": {"chat_history": []}}
            )
            return redirect(url_for("index"))
        return "Invalid credentials!"
    return render_template("login.html")


@app.route("/logout")
def logout():
    if "username" in session:
        if "user_id" in session:
            db_service.users_collection.update_one(
                {"_id": ObjectId(session["user_id"])},
                {"$set": {"chat_history": []}}
            )
        session.pop("username", None)
        session.pop("user_id", None)
    return redirect(url_for("login"))


# REMOVED: /onboarding_dismiss route


# ---------- MAIN APP ROUTES ----------
@app.route("/")
def index():
    if "username" not in session or "user_id" not in session:
        return redirect(url_for("login"))
    return render_template("index.html", username=session["username"])


# REMOVED: /save_personalization route (Optional: Keep if you want API support, but unnecessary without modal)


@app.route("/chat", methods=["POST"])
def chat():
    if "user_id" not in session:
        return jsonify({"reply": "Error: Not logged in"}), 401

    user_id = session["user_id"]

    # === LOCK CHECK ===
    # If the user has already finalized their setup, we reject further chat interactions
    # and instruct the frontend to switch to Dashboard mode.
    user_data_full = db_service.get_user_data(user_id)
    if user_data_full.get("setup_complete", False):
        return jsonify({
            "reply": "Setup is complete. The AI is now disabled. Please use the manual controls to edit your schedule.",
            "action": "lock_ui"
        })
    # ==================

    user_message = request.json.get("message")
    selected_year = request.json.get("year", str(datetime.now().year))

    # === Timezone Fix: Get client_now for planning ===
    client_timestamp_str = request.json.get("client_timestamp")
    client_now = None

    if client_timestamp_str:
        try:
            client_now = datetime.fromisoformat(client_timestamp_str.replace("Z", "+00:00"))
            today_string_with_time = client_now.strftime("%A, %B %d, %Y, %I:%M %p %Z")
        except Exception:
            client_now = datetime.now()
            today_string_with_time = client_now.strftime("%A, %B %d, %Y, %I:%M %p")
    else:
        client_now = datetime.now()
        today_string_with_time = client_now.strftime("%A, %B %d, %Y, %I:%M %p")
    # === End Timezone Fix ===

    # --- TOKEN SAVING IMPLEMENTATION ---
    fresh_context_data = db_service.get_active_context_data(user_id, client_now)

    if not user_data_full or not fresh_context_data:
        session.pop("user_id", None)
        return jsonify({"reply": "Error: Your user data was not found. Please log in again."}), 401

    old_full_history = user_data_full.get("chat_history", [])
    # --- END TOKEN SAVING IMPLEMENTATION ---

    # 2. Standard Chat Message Path (Builds context for AI)
    messages_header = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system",
         "content": f"CRITICAL: The current date and time is {today_string_with_time}. Use this as the anchor for all date/time math."},
        {"role": "user",
         "content": f"Here is my current data. Assume all new dates are for the year {selected_year}. Context: {json.dumps(fresh_context_data)}"}
    ]

    conversational_history = [
                                 msg for msg in old_full_history
                                 if msg.get("role") in ["assistant", "tool"] or
                                    (msg.get("role") == "user" and not msg.get("content", "").startswith(
                                        "Here is my current data."))
                             ][-10:]

    if conversational_history and conversational_history[0].get("role") == "tool":
        conversational_history = conversational_history[1:]

    messages = messages_header + conversational_history
    messages.append({"role": "user", "content": user_message})

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            tools=tools,
            tool_choice="auto"
        )
        response_message = response.choices[0].message

        if response_message.tool_calls:
            messages.append(response_message.model_dump(exclude={'function_call'}))
        else:
            messages.append({"role": response_message.role, "content": response_message.content})

        reply_to_send = ""
        run_planner = False
        planner_response = None
        action_flag = None
        conflict_payload = None  # Storage for modal data

        if response_message.tool_calls:
            for tool_call in response_message.tool_calls:
                function_name = tool_call.function.name
                arguments = json.loads(tool_call.function.arguments)

                func = function_map.get(function_name)

                if not func:
                    response_msg_for_user = "Error: AI tried to call an unknown function."

                elif function_name == "finalize_setup":
                    # 1. Mark DB as complete
                    func(user_id, arguments, client_now)
                    # 2. Trigger Planner
                    run_planner = True
                    # 3. Set Message & Action
                    response_msg_for_user = "Setup complete! I'm now generating your final schedule and locking the manual controls."
                    action_flag = "lock_ui"

                elif function_name == "run_planner_engine":
                    planner_response = func(user_id, arguments, client_now)
                    response_msg_for_user = planner_response.get("message", "OK, I've run the planner.")
                    run_planner = True

                elif function_name == "schedule_recurring_blocks":
                    # schedule_recurring_blocks handles all recurrence logic and triggers the planner internally
                    tool_result = func(user_id, arguments, client_now)

                    # --- NEW LOGIC: Handle Conflict Action ---
                    if isinstance(tool_result, dict) and tool_result.get("action") == "show_conflict_modal":
                        response_msg_for_user = tool_result["message"]
                        action_flag = "show_conflict_modal"
                        conflict_payload = tool_result  # Pass the whole object to frontend
                        # IMPORTANT: Do not set run_planner=True here, as we are waiting for user input
                    # -----------------------------------------
                    elif tool_result.get('status') == 'success':
                        response_msg_for_user = "Study plan successfully generated."
                        # If successful, we might want to check the planner status
                        planner_response = tool_result
                    else:
                        response_msg_for_user = tool_result.get("message")

                else:
                    # Generic DB persistence calls (e.g., save_task)
                    db_result = func(user_id, arguments)
                    response_msg_for_user = map_db_update_response(function_name, db_result, arguments)

                    # --- Check if validation failed (Error string in response) ---
                    if isinstance(response_msg_for_user, str) and response_msg_for_user.startswith("Error:"):
                        run_planner = False  # Do not run planner if date invalid
                    elif function_name in ["save_task", "save_test", "update_task_details", "delete_schedule_item"]:
                        run_planner = True  # For tasks/tests changes, we still trigger the planner

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": function_name,
                    "content": str(response_msg_for_user)
                })
                reply_to_send = response_msg_for_user

        else:
            reply_to_send = response_message.content

        # Fallback to run planner if tasks were saved (e.g., save_task) but not the recurring block
        if run_planner and not planner_response:
            planner_response = planner_engine.run_planner_engine(user_id, {}, now_dt=client_now)
            if planner_response.get("message"):
                # If fallback runs and succeeds, we still use the detailed message here
                reply_to_send += f" (Note: {planner_response['message']})"

        db_service.users_collection.update_one({"_id": ObjectId(user_id)}, {"$set": {"chat_history": messages}})

        # Construct JSON response
        response_payload = {"reply": reply_to_send}
        if action_flag:
            response_payload["action"] = action_flag

        # Inject Conflict Data if flag is set
        if action_flag == "show_conflict_modal" and conflict_payload:
            response_payload["modal_data"] = conflict_payload

        return jsonify(response_payload)

    except Exception as e:
        print(f"Error in /chat route: {e}")
        return jsonify({"reply": "Sorry, I ran into an error. Please try that again."}), 500


@app.route("/get_schedule")
def get_schedule():
    if "user_id" not in session:
        return jsonify({"error": "Not logged in"}), 401

    user_id = session["user_id"]

    client_timestamp_str = request.args.get("client_timestamp")
    client_now = datetime.fromisoformat(
        client_timestamp_str.replace("Z", "+00:00")) if client_timestamp_str else datetime.now()

    db_service.auto_cleanup_past_items(user_id, client_now)

    user_data = db_service.get_user_data(user_id)

    if not user_data:
        return jsonify({"error": "User not found"}), 404

    schedule_data = {
        "schedule": user_data.get("schedule", []),
        "tasks": user_data.get("tasks", []),
        "tests": user_data.get("tests", []),
        "generated_plan": user_data.get("generated_plan", []),
        "preferences": user_data.get("preferences", {}),
        # Removed onboarding_complete
        "setup_complete": user_data.get("setup_complete", False)  # Pass lock status to frontend
    }
    return jsonify(schedule_data)


# app.py

@app.route("/api/manual_save_item", methods=["POST"])
def manual_save_item():
    if "user_id" not in session:
        return jsonify({"error": "Not logged in"}), 401

    user_id = session["user_id"]
    data = request.json

    # 1. Extract Data
    item_type = data.get("type", "assignment")  # default to assignment

    # Map frontend types to backend types if necessary
    # The modal uses 'assignment', 'project', 'seatwork', 'quiz', 'exam'
    # db_service expects 'task' or 'test' bucket logic:
    category = "task"
    if item_type in ["quiz", "exam"]:
        category = "test"
        # For tests, we use 'test_type' and 'date'
        payload = {
            "name": data.get("name"),
            "test_type": item_type,
            "date": data.get("deadline").split("T")[0],  # Extract YYYY-MM-DD
            "priority": data.get("priority", "medium")
        }
    else:
        # For tasks, we use 'task_type' and 'deadline'
        payload = {
            "name": data.get("name"),
            "task_type": item_type,
            "deadline": data.get("deadline"),  # Keeps YYYY-MM-DDTHH:MM
            "priority": data.get("priority", "medium")
        }

    # 2. Save to DB
    try:
        db_service.add_schedule_item(user_id, category, payload)

        # 3. Trigger Planner Engine (Rule-Based Update)
        # We need the current client time to ensure valid scheduling
        client_timestamp_str = data.get("client_timestamp")
        client_now = datetime.fromisoformat(
            client_timestamp_str.replace("Z", "+00:00")) if client_timestamp_str else datetime.now()

        planner_engine.run_planner_engine(user_id, {}, now_dt=client_now)

        return jsonify({"status": "success", "message": "Item saved and schedule updated."})

    except Exception as e:
        print(f"Error in manual_save_item: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/delete_event", methods=["POST"])
def delete_event():
    if "user_id" not in session: return jsonify({"error": "Not logged in"}), 401

    uid = session["user_id"]
    data = request.json

    item_type = data.get("type")  # 'plan', 'task', or 'test'
    name = data.get("title")

    # CASE A: Delete a generated study block (Blue)
    if item_type == "plan":
        date = data.get("start").split("T")[0]
        time_part = data.get("start").split("T")[1][:5]  # HH:MM
        db_service.delete_single_block(uid, name, date, time_part)
        return jsonify({"status": "success", "message": "Study block removed."})

    # CASE B: Delete the actual Task/Test (Red/Orange)
    else:
        # Strip prefixes like "DUE: " or "TEST: "
        clean_name = name.replace("DUE: ", "").replace("TEST: ", "")
        db_service.delete_schedule_item(uid, clean_name)
        # Re-run planner to remove orphan blocks
        planner_engine.run_planner_engine(uid, {}, now_dt=datetime.now())
        return jsonify({"status": "success", "message": f"Task '{clean_name}' and its sessions deleted."})


@app.route("/api/mark_event_done", methods=["POST"])
def mark_event_done():
    if "user_id" not in session: return jsonify({"error": "Not logged in"}), 401
    uid = session["user_id"]
    data = request.json

    # Only applies to 'plan' items
    name = data.get("title")
    date = data.get("start").split("T")[0]
    time_part = data.get("start").split("T")[1][:5]

    db_service.mark_block_done(uid, name, date, time_part)
    return jsonify({"status": "success", "message": "Session marked as done!"})


# --- NEW ROUTE: RESOLVE CONFLICT (SAVE FROM MODAL) ---
@app.route("/api/resolve_conflict", methods=["POST"])
def resolve_conflict():
    if "user_id" not in session: return jsonify({"error": "Unauthorized"}), 401
    uid = session["user_id"]
    data = request.json

    # 1. Parse Client Time
    client_timestamp_str = data.get("client_timestamp")  # Ensure frontend sends this or default to now
    if client_timestamp_str:
        client_now = datetime.fromisoformat(client_timestamp_str.replace("Z", "+00:00"))
    else:
        client_now = datetime.now()

    # 2. Reconstruct Arguments for the Planner
    # Note: Frontend must send 'days', 'start_time', 'end_time', 'item_name'
    args = {
        "item_name": data.get("item_name"),
        "days": data.get("days"),
        "start_time": data.get("start_time"),
        "end_time": data.get("end_time")
    }

    # 3. Force Save (Bypass Detect Conflicts)
    # We call the engine directly, skipping the wrapper check because
    # the user has manually resolved it using the Traffic Lights.
    result = planner_engine.schedule_recurring_blocks(uid, args, client_now)

    return jsonify(result)


# Helper to convert HH:MM to 12-hour format
def format_12hr(time_str):
    try:
        return datetime.strptime(time_str, "%H:%M").strftime("%I:%M %p").lstrip('0')
    except:
        return time_str


class PDF(FPDF):
    def header(self):
        self.set_font('Arial', 'B', 16)
        self.set_text_color(79, 70, 229)
        self.cell(0, 10, 'Your SmartSchedule Weekly Study Guide', 0, 1, 'C')
        self.ln(5)

    def section_title(self, label):
        self.set_font('Arial', 'B', 12)
        self.set_fill_color(243, 244, 246)
        self.set_text_color(31, 41, 55)
        self.cell(0, 10, f" {label}", 0, 1, 'L', True)
        self.ln(2)

    def create_table(self, header, data, col_widths):
        self.set_fill_color(79, 70, 229)
        self.set_text_color(255)
        self.set_font('Arial', 'B', 10)
        for i, column in enumerate(header):
            # The '✓' symbol is rendered using the standard encoding if supported,
            # or you can use a fallback string if your environment lacks the font.
            self.cell(col_widths[i], 8, column, 1, 0, 'C', True)
        self.ln()

        self.set_text_color(0)
        self.set_font('Arial', '', 9)
        for row in data:
            if self.get_y() > 260:
                self.add_page()
            for i, item in enumerate(row):
                # Centered alignment for all columns as requested
                self.cell(col_widths[i], 8, str(item), 1, 0, 'C')
            self.ln()
        self.ln(5)


@app.route("/api/generate_pdf_and_clear")
def generate_pdf_and_clear():
    if "user_id" not in session: return redirect(url_for('login'))
    uid = session["user_id"]
    user_data = db_service.get_user_data(uid)

    pdf = PDF()
    pdf.add_page()

    # --- SECTION 1: RECURRING CLASSES ---
    if user_data.get("schedule"):
        pdf.section_title("1. Weekly Recurring Classes")
        header = ['Day', 'Subject', 'Time Slot']
        data = [[c['day'], c['subject'], f"{format_12hr(c['start_time'])} - {format_12hr(c['end_time'])}"] for c in
                user_data["schedule"]]
        pdf.create_table(header, data, [35, 75, 80])

    # --- SECTION 2: STUDY PLAN WITH UPDATED HEADERS ---
    plan = user_data.get("generated_plan", [])
    if plan:
        pdf.section_title("2. Personalized Study Checklist")

        weeks = {}
        for item in plan:
            dt = datetime.strptime(item['date'], "%Y-%m-%d")
            monday = dt - timedelta(days=dt.weekday())
            sunday = monday + timedelta(days=6)
            week_label = f"{monday.strftime('%b %d')} - {sunday.strftime('%b %d, %Y')}"
            week_key = monday.strftime("%Y-%m-%d")

            if week_key not in weeks:
                weeks[week_key] = {"label": week_label, "items": []}
            weeks[week_key]["items"].append(item)

        all_meta = user_data.get("tasks", []) + user_data.get("tests", [])

        for week_key in sorted(weeks.keys()):
            pdf.set_font('Arial', 'B', 10)
            pdf.cell(0, 8, f"Timeline: {weeks[week_key]['label']}", 0, 1)

            # REVISED HEADER: Using the checkmark symbol
            header = ['Done [ / ] ', 'Date', 'Duration', 'Task Name']
            # Note: chr(118) in some encodings or direct '✓' can be used depending on your FPDF font setup.

            data = []
            for i in weeks[week_key]["items"]:
                row_date = datetime.strptime(i['date'], "%Y-%m-%d").strftime("%a, %b %d")
                duration = f"{format_12hr(i['start_time'])} - {format_12hr(i['end_time'])}"

                # Reconstruct Name: "Work on [Type] - [Subject]"
                raw_subject = i['task'].replace("Work on ", "")
                meta = next((m for m in all_meta if m['name'] == raw_subject), None)
                item_type = "Task"
                if meta:
                    item_type = meta.get('task_type') or meta.get('test_type') or "Study"

                formatted_task_name = f"Work on {item_type.title()} - {raw_subject}"
                data.append(["[  ]", row_date, duration, formatted_task_name])

            pdf.create_table(header, data, [25, 35, 55, 75])

    # --- GENERATE AND CLEAR ---
    output = io.BytesIO()
    pdf_output = pdf.output(dest='S').encode('latin-1', 'replace')  # Ensure encoding fallback
    output.write(pdf_output)
    output.seek(0)

    db_service.clear_user_schedule(uid)  #

    return send_file(output, as_attachment=True, download_name="Study_Checklist.pdf", mimetype="application/pdf")


if __name__ == "__main__":
    app.run(debug=True)