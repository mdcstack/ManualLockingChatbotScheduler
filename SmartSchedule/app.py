from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from pymongo import MongoClient
from dotenv import load_dotenv, find_dotenv
from flask_bcrypt import Bcrypt
from openai import OpenAI
from bson.objectid import ObjectId
import os
import json
from datetime import datetime
from db_service import DBService
from planner_engine import PlannerEngine

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

# === CONCISE SYSTEM PROMPT (FIXED for Recurrence) ===

SYSTEM_PROMPT = """
You are a 'Study Schedule Maker' assistant. Your sole goal is to translate user requests into structured function calls to manage their schedule.

**CORE DIRECTIVES (NON-NEGOTIABLE):**
1.  **SEQUENCING (CRITICAL FIX):** If the user mentions a new item or deadline (e.g., 'midterm', 'assignment'), you MUST call `save_task` or `save_test` first to create the item. 
2.  **RECURRENCE (LOW COST):** If the user provides recurring study times (e.g., "Mondays and Wednesdays from 4 PM to 6 PM"), you MUST immediately follow up with a single call to `schedule_recurring_blocks` to capture ALL recurring study times in one step.
3.  **TIME ANCHOR:** The current date and time is provided via a system message ("CRITICAL: The current date and time is [Date & Time]"). You MUST use this to resolve dates.
4.  **DATE OUTPUT FORMAT:** Output dates in **YYYY-MM-DD** format and time in **HH:MM** format for all tools.
5.  **REJECTION RULE:** If the deadline is *before* the current time, respond: "Sorry, I can't add items for dates and times that have already passed."
6.  **PLANNER FLOW:** After saving any task, test, or specific study block, you MUST call the `run_planner_engine()` tool to immediately regenerate the schedule.
"""

# === VASTLY SIMPLIFIED TOOL DEFINITION (Updated for Recurrence) ===
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
            "name": "run_planner_engine",
            "description": "Runs the full schedule validator and consolidation engine.",
            "parameters": {"type": "object", "properties": {}}
        }
    }
]


# ----------------- TOOL EXECUTION MAPPING -----------------
def map_db_update_response(func_name, result, args):
    """Generates a user-friendly response message for DB operations."""
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
    "save_task": lambda uid, args: db_service.add_schedule_item(uid, "task", args),
    "save_test": lambda uid, args: db_service.add_schedule_item(uid, "test", args),
    "update_task_details": lambda uid, args: db_service.update_task_details(uid, args),
    "update_class_schedule": lambda uid, args: db_service.update_class_schedule(uid, args),
    "delete_schedule_item": lambda uid, args: db_service.delete_schedule_item(uid, args.get("item_name")),

    # New Tool Mapping - Recurring Blocks
    "schedule_recurring_blocks": lambda uid, args, now_dt: planner_engine.schedule_recurring_blocks(uid, args, now_dt),

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
            "onboarding_complete": False
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


# --- API ROUTE TO DISMISS ONBOARDING ---
@app.route("/onboarding_dismiss", methods=["POST"])
def onboarding_dismiss():
    if "user_id" not in session:
        return jsonify({"error": "Not logged in"}), 401

    user_id = session["user_id"]
    db_service.set_onboarding_complete(user_id, True)
    return jsonify({"status": "acknowledged"})


# ---------- MAIN APP ROUTES ----------
@app.route("/")
def index():
    if "username" not in session or "user_id" not in session:
        return redirect(url_for("login"))
    return render_template("index.html", username=session["username"])


@app.route("/save_personalization", methods=["POST"])
def save_personalization():
    if "user_id" not in session:
        return jsonify({"error": "Not logged in"}), 401

    user_id = session["user_id"]
    data = request.json

    try:
        client_timestamp_str = request.json.get("client_timestamp")
        client_now = datetime.fromisoformat(
            client_timestamp_str.replace("Z", "+00:00")) if client_timestamp_str else datetime.now()

        preferences = data.get("preferences", {})
        db_service.update_user_preference(user_id, preferences)

        db_service.set_onboarding_complete(user_id, True)

        # Run planner after saving preferences
        planner_response = planner_engine.run_planner_engine(user_id, {}, now_dt=client_now)

        return jsonify({"reply": f"Settings saved! {planner_response['message']}"})

    except Exception as e:
        print(f"Error in /save_personalization: {e}")
        return jsonify({"reply": "Sorry, there was an error saving your settings."}), 500


@app.route("/chat", methods=["POST"])
def chat():
    if "user_id" not in session:
        return jsonify({"reply": "Error: Not logged in"}), 401

    user_message = request.json.get("message")
    user_id = session["user_id"]
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
    user_data_full = db_service.get_user_data(user_id)
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

        if response_message.tool_calls:
            for tool_call in response_message.tool_calls:
                function_name = tool_call.function.name
                arguments = json.loads(tool_call.function.arguments)

                func = function_map.get(function_name)

                if not func:
                    response_msg_for_user = "Error: AI tried to call an unknown function."

                elif function_name == "run_planner_engine":
                    planner_response = func(user_id, arguments, client_now)
                    response_msg_for_user = planner_response.get("message", "OK, I've run the planner.")
                    run_planner = True

                elif function_name == "schedule_recurring_blocks":
                    # schedule_recurring_blocks handles all recurrence logic and triggers the planner internally
                    planner_response = func(user_id, arguments, client_now)

                    # --- MESSAGE SUPPRESSION FIX APPLIED HERE ---
                    if planner_response.get('status') == 'success':
                        response_msg_for_user = "Study plan successfully generated."
                    else:
                        # If planner fails, return the detailed error message
                        response_msg_for_user = planner_response.get("message")
                    # --- END FIX ---

                else:
                    # Generic DB persistence calls (e.g., save_task)
                    db_result = func(user_id, arguments)
                    response_msg_for_user = map_db_update_response(function_name, db_result, arguments)

                    if function_name in ["save_task", "save_test", "update_task_details", "delete_schedule_item"]:
                        run_planner = True  # For tasks/tests changes, we still trigger the planner

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": function_name,
                    "content": response_msg_for_user
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

        return jsonify({"reply": reply_to_send})

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
        "onboarding_complete": user_data.get("onboarding_complete", False)
    }
    return jsonify(schedule_data)


if __name__ == "__main__":
    app.run(debug=True)