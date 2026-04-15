import discord
import os
import re
import json
import random
from difflib import get_close_matches
from dotenv import load_dotenv
from datetime import datetime, timedelta
import pytz
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import dateparser

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage, AIMessage
from langchain_core.tools import tool
from langchain_groq import ChatGroq
from langgraph.graph import StateGraph, END
from typing import TypedDict, Annotated
from langgraph.graph.message import add_messages
from langgraph.checkpoint.memory import MemorySaver

# =====================
# ENV
# =====================
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# =====================
# TIMEZONE (IST)
# =====================
IST = pytz.timezone('Asia/Kolkata')

def now_ist():
    """Get current time in IST (Indian Standard Time)."""
    return datetime.now(IST)

# Dateparser settings — always parse times as IST
DATEPARSER_SETTINGS = {
    'TIMEZONE': 'Asia/Kolkata',
    'RETURN_AS_TIMEZONE_AWARE': True,
    'PREFER_DATES_FROM': 'future'
}

# Track last interaction to allow conversation continuation without saying "aurora" every time
active_users = {}

# =====================
# GOOGLE SHEETS
# =====================
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS"))

creds = ServiceAccountCredentials.from_json_keyfile_dict(
    creds_dict, scope
)

client_gs = gspread.authorize(creds)
spreadsheet = client_gs.open_by_key(SPREADSHEET_ID)

def get_user_sheet(username):
    name = username.lower().replace("#", "_").replace(" ", "_")
    sheet_name = f"{name}_tasks"

    try:
        return spreadsheet.worksheet(sheet_name)
    except:
        ws = spreadsheet.add_worksheet(title=sheet_name, rows="1000", cols="4")
        ws.append_row(["Date", "User", "Task", "Status"])
        return ws

def get_reminders_sheet():
    """Get or create the 'reminders' sheet in Google Sheets."""
    try:
        return spreadsheet.worksheet("reminders")
    except:
        ws = spreadsheet.add_worksheet(title="reminders", rows="1000", cols="6")
        ws.append_row(["User", "User ID", "Date", "Reminder", "Type", "Time"])
        return ws

def get_users_sheet():
    """Get or create the 'aurora_users' sheet to track registered users."""
    try:
        return spreadsheet.worksheet("aurora_users")
    except:
        ws = spreadsheet.add_worksheet(title="aurora_users", rows="100", cols="3")
        ws.append_row(["Username", "User ID", "Registered Date"])
        return ws

def register_user(username, user_id):
    """Register a user in the tracking sheet if not already present."""
    try:
        sheet = get_users_sheet()
        rows = sheet.get_all_values()
        for row in rows[1:]:
            if len(row) >= 2 and row[1] == str(user_id):
                return  # Already registered
        sheet.append_row([str(username), str(user_id), now_ist().strftime("%Y-%m-%d")])
        print(f"Registered new user: {username} ({user_id})")
    except Exception as e:
        print(f"Error registering user: {e}")

def get_all_tracked_users():
    """Get all tracked users from the aurora_users sheet."""
    try:
        sheet = get_users_sheet()
        rows = sheet.get_all_values()
        users = []
        for row in rows[1:]:
            if len(row) >= 2 and row[1]:
                users.append((row[0], int(row[1])))
        return users
    except Exception as e:
        print(f"Error getting tracked users: {e}")
        return []

# =====================
# PROACTIVE FEATURES REGISTRY
# =====================
# Master registry of all proactive features Aurora can send.
# Each key is the feature ID, value describes it for both internal logic and LLM awareness.
PROACTIVE_FEATURES = {
    "morning_greeting": {
        "name": "Morning Greeting",
        "time": "8:00 AM",
        "emoji": "☀️",
        "description": "Warm good morning message with today's task list",
        "job_ids": ["morning_greeting"],
    },
    "midday_checkin": {
        "name": "Midday Check-ins",
        "time": "12:00 PM & 4:00 PM",
        "emoji": "🕐",
        "description": "Task progress updates at noon and 4 PM",
        "job_ids": ["midday_checkin_12pm", "midday_checkin_4pm"],
    },
    "hydration": {
        "name": "Hydration Reminders",
        "time": "Every ~2hrs (10 AM – 12 AM) with random jitter",
        "emoji": "💧",
        "description": "Random water drinking reminders throughout the day",
        "job_ids": ["hydration_10am", "hydration_12pm", "hydration_2pm", "hydration_4pm",
                    "hydration_6pm", "hydration_8pm", "hydration_10pm", "hydration_12am"],
    },
    "walk_reminder": {
        "name": "Walk Reminder",
        "time": "5:30 PM",
        "emoji": "🚶",
        "description": "Evening walk suggestion (5:30 – 7:00 PM window)",
        "job_ids": ["walk_reminder"],
    },
    "work_session": {
        "name": "Work Session",
        "time": "7:30 PM",
        "emoji": "💻",
        "description": "Suggests starting a focused work session with pending tasks",
        "job_ids": ["work_session"],
    },
    "night_summary": {
        "name": "Night Summary",
        "time": "12:00 AM (midnight)",
        "emoji": "🌙",
        "description": "End-of-day recap showing completed vs incomplete tasks, offers to reschedule",
        "job_ids": ["night_summary"],
    },
}

# In-memory cache of proactive settings (feature_id -> True/False)
proactive_settings = {}

def get_settings_sheet():
    """Get or create the 'aurora_settings' sheet for persistent toggle states."""
    try:
        return spreadsheet.worksheet("aurora_settings")
    except:
        ws = spreadsheet.add_worksheet(title="aurora_settings", rows="100", cols="2")
        ws.append_row(["Setting", "Value"])
        # Default all features to ON
        for key in PROACTIVE_FEATURES:
            ws.append_row([key, "on"])
        return ws

def load_proactive_settings():
    """Load proactive feature settings from Google Sheet into memory."""
    global proactive_settings
    try:
        sheet = get_settings_sheet()
        rows = sheet.get_all_values()
        found_keys = set()
        for row in rows[1:]:
            if len(row) >= 2:
                proactive_settings[row[0]] = (row[1].lower() == "on")
                found_keys.add(row[0])
        # Ensure all features exist in sheet (in case new ones were added)
        for key in PROACTIVE_FEATURES:
            if key not in found_keys:
                proactive_settings[key] = True
                sheet.append_row([key, "on"])
        print(f"Loaded proactive settings: {proactive_settings}")
    except Exception as e:
        print(f"Error loading proactive settings: {e}")
        # Default everything to on
        for key in PROACTIVE_FEATURES:
            proactive_settings[key] = True

def save_proactive_setting(key, enabled):
    """Save a single proactive setting to Google Sheet and update cache."""
    proactive_settings[key] = enabled
    value_str = "on" if enabled else "off"
    try:
        sheet = get_settings_sheet()
        rows = sheet.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if len(row) >= 1 and row[0] == key:
                sheet.update([[key, value_str]], range_name=f"A{i}:B{i}")
                return
        # Not found, add new row
        sheet.append_row([key, value_str])
    except Exception as e:
        print(f"Error saving proactive setting '{key}': {e}")

def is_feature_enabled(feature_id):
    """Check if a proactive feature is currently enabled."""
    return proactive_settings.get(feature_id, True)

# =====================
# REMINDER SYSTEM
# =====================
scheduler = AsyncIOScheduler(timezone=IST)

async def get_channel_robust(channel_id):
    """Attempt to get channel from cache, then fetch from API if not found."""
    channel = client.get_channel(channel_id)
    if channel:
        return channel
    try:
        print(f"Channel {channel_id} not in cache, fetching from API...")
        return await client.fetch_channel(channel_id)
    except Exception as e:
        print(f"Error fetching channel {channel_id}: {e}")
        return None

async def send_reminder(channel_id, text, user_id, reminder_type="once"):
    """Send a reminder and tag the user. Auto-remove one-time reminders from the sheet."""
    channel = await get_channel_robust(channel_id)
    if channel:
        mention = f"<@{user_id}>"
        await channel.send(f"⏰ {mention} **Reminder:** {text}")
        print(f"Reminder sent: '{text}' to <@{user_id}>")
        active_users[user_id] = now_ist()
    else:
        print(f"Failed to send reminder '{text}': Channel not found.")

    # Auto-remove one-time reminders from the sheet after firing
    if reminder_type == "once":
        try:
            sheet = get_reminders_sheet()
            rows = sheet.get_all_values()
            for i, row in enumerate(rows[1:], start=2):
                # Match by user_id, reminder text, and type
                if row[1] == str(user_id) and row[3] == text and row[4].lower() == "once":
                    sheet.delete_rows(i)
                    print(f"Auto-removed one-time reminder: '{text}' for user {user_id}")
                    break
        except Exception as e:
            print(f"Error removing one-time reminder from sheet: {e}")

def schedule_reminder(channel_id, text, run_time, repeat="once", user_id=None):
    """Schedule a reminder with APScheduler. Supports once, daily, weekly. Returns job_id."""
    # Generate a unique job ID based on user and content to allow deletion later
    safe_text = re.sub(r'[^a-zA-Z0-9]', '_', text[:30].lower())
    job_id = f"user_{user_id}_{safe_text}_{run_time.strftime('%H%M')}"

    if repeat == "daily":
        scheduler.add_job(
            send_reminder,
            "cron",
            hour=run_time.hour,
            minute=run_time.minute,
            args=[channel_id, text, user_id, "daily"],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=600
        )
    elif repeat == "weekly":
        scheduler.add_job(
            send_reminder,
            "cron",
            day_of_week=run_time.strftime("%a").lower()[:3],
            hour=run_time.hour,
            minute=run_time.minute,
            args=[channel_id, text, user_id, "weekly"],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=600
        )
    else:
        # One-time reminder
        scheduler.add_job(
            send_reminder,
            "date",
            run_date=run_time,
            args=[channel_id, text, user_id, "once"],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=600
        )
    return job_id

def load_reminders_from_sheet():
    """Load all reminders from the Google Sheet and reschedule them on startup."""
    try:
        sheet = get_reminders_sheet()
        rows = sheet.get_all_values()[1:]  # Skip header

        count = 0
        for row in rows:
            if len(row) < 6:
                continue
            user, user_id, date, reminder, rtype, time_str = row[:6]
            parsed_time = dateparser.parse(time_str, settings=DATEPARSER_SETTINGS)

            if not parsed_time:
                continue

            rtype_lower = rtype.lower().strip()

            # For one-time reminders, only schedule if they're in the future
            if rtype_lower == "once":
                if parsed_time < now_ist():
                    # Already past — remove from sheet
                    try:
                        idx = rows.index(row) + 2  # +2 for header and 0-index
                        sheet.delete_rows(idx)
                        print(f"Removed expired one-time reminder: '{reminder}'")
                    except:
                        pass
                    continue

            schedule_reminder(CHANNEL_ID, reminder, parsed_time, rtype_lower, int(user_id))
            count += 1

        print(f"Loaded {count} reminders from Google Sheet.")
    except Exception as e:
        print(f"Error loading reminders from sheet: {e}")

# =====================
# PROACTIVE AGENT MESSAGES
# =====================
async def morning_greeting():
    """Send a warm morning greeting with today's tasks at 8:00 AM IST."""
    if not is_feature_enabled("morning_greeting"):
        print("Skipping morning greeting: Feature disabled.")
        return

    channel = await get_channel_robust(CHANNEL_ID)
    if not channel:
        print("Skipping morning greeting: Channel not found.")
        return

    users = get_all_tracked_users()
    print(f"Starting morning greeting for {len(users)} users...")
    current_time = now_ist()
    # ... (rest of function logic)

    greetings = [
        "Rise and shine! ☀️",
        "Good morning, sunshine! 🌅",
        "A brand new day awaits! 🌸",
        "Top of the morning to you! ✨",
        "Wakey wakey! Time to make today amazing! 🌞",
        "Good morning! The world is full of possibilities today! 🌈",
    ]

    for user_name, user_id in users:
        try:
            sheet = get_user_sheet(user_name)
            rows = sheet.get_all_values()

            pending_tasks = []
            for r in rows[1:]:
                if len(r) >= 4 and r[3].lower() in ("pending", "incomplete"):
                    pending_tasks.append(r[2])

            msg = f"{random.choice(greetings)} <@{user_id}>\n\n"
            msg += f"📅 **{current_time.strftime('%A, %B %d, %Y')}**\n\n"

            if pending_tasks:
                msg += "📋 **Here's what's on your plate today:**\n"
                for i, task in enumerate(pending_tasks, 1):
                    msg += f"  {i}. {task}\n"
                msg += f"\nYou've got **{len(pending_tasks)}** task(s) to tackle. Let's crush it! 💪"
            else:
                msg += "🎯 You have a clean slate today! Want to plan some tasks? Just let me know 📝"

            msg += "\n\n_Remember: consistency beats intensity. One step at a time!_ 🚶‍♂️"

            await channel.send(msg)
            active_users[user_id] = now_ist()
        except Exception as e:
            print(f"Error sending morning greeting to {user_name}: {e}")


async def midday_checkin():
    """Send a midday check-in on task progress."""
    if not is_feature_enabled("midday_checkin"):
        print("Skipping midday check-in: Feature disabled.")
        return

    channel = await get_channel_robust(CHANNEL_ID)
    if not channel:
        print("Skipping midday check-in: Channel not found.")
        return

    users = get_all_tracked_users()
    print(f"Starting midday check-in for {len(users)} users...")
    current_time = now_ist()
    # ... (rest of function logic)
    today = current_time.strftime("%Y-%m-%d")

    for user_name, user_id in users:
        try:
            sheet = get_user_sheet(user_name)
            rows = sheet.get_all_values()

            done_today = 0
            pending = 0
            for r in rows[1:]:
                if len(r) >= 4:
                    if r[3].lower() == "done" and r[0] == today:
                        done_today += 1
                    elif r[3].lower() in ("pending", "incomplete"):
                        pending += 1

            if done_today == 0 and pending == 0:
                continue  # Skip users with no tasks

            msg = f"🕐 **Midday Check-in** — <@{user_id}>\n\n"

            if done_today > 0:
                msg += f"✅ You've completed **{done_today}** task(s) so far. Nice work!\n"
            if pending > 0:
                msg += f"📌 **{pending}** task(s) still pending. You've got this — keep the momentum going! 🚀"
            elif pending == 0 and done_today > 0:
                msg += "🎉 All tasks done before lunch? You're on fire! 🔥"

            await channel.send(msg)
            active_users[user_id] = now_ist()
        except Exception as e:
            print(f"Error sending midday check-in to {user_name}: {e}")


async def hydration_reminder():
    """Send periodic hydration reminders."""
    if not is_feature_enabled("hydration"):
        print("Skipping hydration reminder: Feature disabled.")
        return

    channel = await get_channel_robust(CHANNEL_ID)
    if not channel:
        print("Skipping hydration reminder: Channel not found.")
        return

    print("Sending hydration reminder...")
    # ... (rest of function logic)

    messages = [
        "💧 Quick hydration check — have you sipped some water recently? Your body will thank you! 🌊",
        "💧 Time for a water break! Staying hydrated keeps your mind sharp and focused! 🧠✨",
        "💧 Hey! Don't forget your water! Dehydration is sneaky — drink up! 🥤",
        "💧 Water reminder! A hydrated brain is a productive brain. Take a sip! 💪",
        "💧 Pause, breathe, and drink some water. Your future self will appreciate it! 🙏",
        "💧 Hydration o'clock! Even mild dehydration can zap your energy. Sip sip! 🚰",
    ]

    await channel.send(random.choice(messages))


async def walk_reminder():
    """Send a walk reminder at 5:30 PM IST."""
    if not is_feature_enabled("walk_reminder"):
        print("Skipping walk reminder: Feature disabled.")
        return

    channel = await get_channel_robust(CHANNEL_ID)
    if not channel:
        print("Skipping walk reminder: Channel not found.")
        return

    print("Sending walk reminder...")
    # ... (rest of logic)

    messages = [
        "🚶‍♂️ It's the golden hour! Perfect time for an evening walk (5:30 – 7:00 PM). Fresh air, clear mind! 🌅🍃",
        "🌆 Hey! Step outside for a walk — the evening breeze is calling! Nature recharges you better than any screen 🌿",
        "🏃 Evening walk time! Your body and mind deserve a break. Even 15 minutes makes a huge difference! 🌅",
        "🚶 Time to stretch those legs! The 5:30 – 7:00 PM window is ideal for a refreshing walk. Go soak in the sunset! 🌇",
    ]

    await channel.send(random.choice(messages))


async def work_session_reminder():
    """Suggest starting a work session at 7:30 PM IST."""
    if not is_feature_enabled("work_session"):
        print("Skipping work session reminder: Feature disabled.")
        return

    channel = await get_channel_robust(CHANNEL_ID)
    if not channel:
        print("Skipping work session reminder: Channel not found.")
        return

    users = get_all_tracked_users()
    print(f"Starting work session suggestions for {len(users)} users...")
    # ... (rest of logic)

    for user_name, user_id in users:
        try:
            sheet = get_user_sheet(user_name)
            rows = sheet.get_all_values()

            pending_tasks = []
            for r in rows[1:]:
                if len(r) >= 4 and r[3].lower() == "pending":
                    pending_tasks.append(r[2])

            msg = f"💻 <@{user_id}> It's a great time to settle in for a focused work session! 🎯\n\n"

            if pending_tasks:
                msg += "Here's what's still on your list:\n"
                for i, task in enumerate(pending_tasks, 1):
                    msg += f"  {i}. {task}\n"
                msg += f"\n🔥 **{len(pending_tasks)} task(s) remaining.** Pick one and start — momentum will carry you!"
            else:
                msg += "All your tasks are done! 🎉 Want to work on personal projects or learning goals? This is a great time!"

            msg += "\n\n_Tip: Start with the easiest task to build momentum, or tackle the hardest while you're fresh!_ 🧠"

            await channel.send(msg)
            active_users[user_id] = now_ist()
        except Exception as e:
            print(f"Error sending work reminder to {user_name}: {e}")


async def night_summary():
    """Send an end-of-day summary at midnight IST and ask about rescheduling."""
    if not is_feature_enabled("night_summary"):
        print("Skipping night summary: Feature disabled.")
        return

    channel = await get_channel_robust(CHANNEL_ID)
    if not channel:
        print("Skipping night summary: Channel not found.")
        return

    users = get_all_tracked_users()
    print(f"Starting night summary for {len(users)} users...")
    current_time = now_ist()
    # ... (rest of logic)
    # At midnight, "today" is technically the new day, so look at yesterday's tasks
    yesterday = (current_time - timedelta(days=1)).strftime("%Y-%m-%d")
    today = current_time.strftime("%Y-%m-%d")

    for user_name, user_id in users:
        try:
            sheet = get_user_sheet(user_name)
            rows = sheet.get_all_values()

            completed = []
            incomplete = []

            for i, r in enumerate(rows[1:], start=2):
                if len(r) >= 4:
                    task_date = r[0]
                    task_name = r[2]
                    status = r[3].lower()

                    # Count tasks completed yesterday (since this runs at midnight)
                    if status == "done" and task_date == yesterday:
                        completed.append(task_name)
                    elif status in ("pending", "incomplete"):
                        incomplete.append(task_name)

            msg = f"🌙 **End of Day Summary** — <@{user_id}>\n"
            msg += f"📅 {(current_time - timedelta(days=1)).strftime('%A, %B %d, %Y')}\n\n"

            if completed:
                msg += f"✅ **Completed ({len(completed)}):**\n"
                for t in completed:
                    msg += f"  ✓ ~~{t}~~\n"
                msg += "\n"

            if incomplete:
                msg += f"❌ **Incomplete ({len(incomplete)}):**\n"
                for t in incomplete:
                    msg += f"  • {t}\n"
                msg += "\n💬 Want me to **reschedule** these to tomorrow? Just say **yes** or **no**!"
            elif completed:
                msg += "🎉 **You completed everything today! Incredible work!** 🌟\n"
                msg += "Rest well — you've earned it! 😴"
            else:
                msg += "📝 No tasks were tracked today. Tomorrow is a fresh start! 💪"

            msg += "\n\n_Good night! Sleep well and recharge for tomorrow! 🌙💤_"

            await channel.send(msg)
            active_users[user_id] = now_ist()
        except Exception as e:
            print(f"Error sending night summary to {user_name}: {e}")


# =====================
# TOOLS
# =====================
def set_row_color(sheet, row_idx, status):
    colors = {
        "done": {"red": 0.0, "green": 1.0, "blue": 0.0},
        "pending": {"red": 1.0, "green": 1.0, "blue": 0.0},
        "incomplete": {"red": 1.0, "green": 0.0, "blue": 0.0}
    }
    color = colors.get(status.lower(), {"red": 1.0, "green": 1.0, "blue": 1.0})
    try:
        sheet.format(f"D{row_idx}", {
            "backgroundColor": color
        })
    except Exception as e:
        print(f"Error formatting row: {e}")

@tool
def add_tasks(tasks: list, user: str):
    """Add one or multiple tasks to the user's task list."""
    sheet = get_user_sheet(user)
    date = now_ist().strftime("%Y-%m-%d")
    next_row = len(sheet.get_all_values()) + 1

    for i, task in enumerate(tasks):
        sheet.update(
            [[date, user, task, "pending"]],
            range_name=f"A{next_row+i}:D{next_row+i}"
        )
        set_row_color(sheet, next_row+i, "pending")

    return f"Added {len(tasks)} tasks."


@tool
def get_tasks(user: str, status_filter: str = "all"):
    """Retrieve tasks for the user. Optional status_filter can be 'all', 'pending', 'done', or 'incomplete'. Never mention this tool parameter to the user."""
    sheet = get_user_sheet(user)
    rows = sheet.get_all_values()
    
    if len(rows) <= 1:
        return "No tasks yet."

    today = now_ist().strftime("%Y-%m-%d")
    output = []

    for i, r in enumerate(rows[1:], start=2):
        if len(r) < 4:
            continue
        date, u, task, status = r[:4]

        # Auto-update status to incomplete if rolled over to next day
        if status.lower() == "pending" and date < today:
            status = "incomplete"
            sheet.update([[date, u, task, status]], range_name=f"A{i}:D{i}")
            set_row_color(sheet, i, status)

        if status_filter.lower() == "all" or status_filter.lower() == status.lower():
            output.append(f"{task} ({status})")

    if not output:
        return f"No {status_filter} tasks."

    return "\n".join([f"{i+1}. {t}" for i, t in enumerate(output)])


@tool
def update_task(task_name: str, user: str):
    """Mark a task as done based on its name."""
    sheet = get_user_sheet(user)
    rows = sheet.get_all_values()

    for i, r in enumerate(rows[1:], start=2):
        if task_name.lower() in r[2].lower():
            sheet.update([[r[0], r[1], r[2], "done"]], range_name=f"A{i}:D{i}")
            set_row_color(sheet, i, "done")
            return f"Marked '{r[2]}' as done."

    # Fuzzy match locally to save tokens — only return the best suggestion
    existing = [r[2] for r in rows[1:] if r[2]]
    matches = get_close_matches(task_name.lower(), [t.lower() for t in existing], n=1, cutoff=0.4)
    if matches:
        original = existing[[t.lower() for t in existing].index(matches[0])]
        return f"Task '{task_name}' not found. Did you mean: '{original}'?"
    return "Task not found. No similar tasks exist."


@tool
def delete_task(task_name: str, user: str):
    """Delete a task from the user's list."""
    sheet = get_user_sheet(user)
    rows = sheet.get_all_values()

    for i, r in enumerate(rows[1:], start=2):
        if task_name.lower() in r[2].lower():
            sheet.delete_rows(i)
            return f"Deleted '{r[2]}'."

    # Fuzzy match locally to save tokens — only return the best suggestion
    existing = [r[2] for r in rows[1:] if r[2]]
    matches = get_close_matches(task_name.lower(), [t.lower() for t in existing], n=1, cutoff=0.4)
    if matches:
        original = existing[[t.lower() for t in existing].index(matches[0])]
        return f"Task '{task_name}' not found. Did you mean: '{original}'?"
    return "Task not found. No similar tasks exist."


@tool
def schedule_reminder_tool(time: str, task: str, repeat: str, user: str, user_id: str):
    """Schedule a reminder for a task at a specific time.
    Use repeat='daily' for daily reminders, repeat='weekly' for weekly reminders, or repeat='once' for one-time reminders.
    The time can be natural language like 'tomorrow at 9am', '5pm', 'in 2 hours', etc.
    user_id is the Discord user ID number for tagging.
    """
    parsed_time = dateparser.parse(time, settings=DATEPARSER_SETTINGS)

    if not parsed_time:
        return "Couldn't understand the time."

    # Save to Google Sheets
    try:
        sheet = get_reminders_sheet()
        sheet.append_row([
            user,
            str(user_id),
            now_ist().strftime("%Y-%m-%d"),
            task,
            repeat,
            parsed_time.strftime("%Y-%m-%d %H:%M:%S")
        ])
    except Exception as e:
        return f"Error saving reminder to sheet: {str(e)}"

    # Schedule with APScheduler
    schedule_reminder(
        CHANNEL_ID,
        task,
        parsed_time,
        repeat,
        int(user_id)
    )

    if repeat == "daily":
        return f"Daily reminder set for '{task}' at {parsed_time.strftime('%I:%M %p')} every day."
    elif repeat == "weekly":
        return f"Weekly reminder set for '{task}' at {parsed_time.strftime('%I:%M %p')} every {parsed_time.strftime('%A')}."
    else:
        return f"One-time reminder set for '{task}' at {parsed_time.strftime('%I:%M %p on %B %d, %Y')}."

@tool
def get_reminders(user_id: str):
    """Retrieve a list of all active reminders for the user. user_id must be the Discord user ID."""
    try:
        sheet = get_reminders_sheet()
        rows = sheet.get_all_values()[1:]
        
        user_reminders = []
        for r in rows:
            if len(r) >= 6 and r[1] == str(user_id):
                 user_reminders.append(f"{r[3]} ({r[4]} at {r[5]})")
                 
        if not user_reminders:
            return "You have no active reminders."
            
        return "\n".join([f"{idx+1}) {rem}" for idx, rem in enumerate(user_reminders)])
    except Exception as e:
        return f"Error fetching reminders: {e}"


@tool
def delete_reminder(reminder_name: str, user_id: str):
    """Delete/cancel a user-created reminder by its name. Searches both the saved reminders and the live scheduler.
    reminder_name: the name or keyword of the reminder to delete (e.g. 'call mom', 'water', 'wake guneet').
    user_id: Discord user ID.
    """
    deleted_from_sheet = 0
    # 1. Delete ALL matching rows from Google Sheets
    try:
        sheet = get_reminders_sheet()
        rows = sheet.get_all_values()
        # Iterate in reverse to avoid index shifting when deleting
        for i in range(len(rows) - 1, 0, -1):
            row = rows[i]
            if len(row) >= 4 and row[1] == str(user_id) and reminder_name.lower() in row[3].lower():
                sheet.delete_rows(i + 1)  # +1 because sheet rows are 1-indexed
                deleted_from_sheet += 1
    except Exception as e:
        print(f"Error deleting from sheet: {e}")

    # 2. Remove matching jobs from live scheduler
    removed_jobs = 0
    for job in list(scheduler.get_jobs()):
        if f"user_{user_id}" in job.id:
            # Check if reminder name matches the job ID or args
            job_args_str = str(job.args).lower() if job.args else ""
            if reminder_name.lower() in job.id.lower() or reminder_name.lower() in job_args_str:
                try:
                    scheduler.remove_job(job.id)
                    removed_jobs += 1
                except:
                    pass

    if deleted_from_sheet > 0 or removed_jobs > 0:
        return f"Deleted {deleted_from_sheet} reminder(s) matching '{reminder_name}' and stopped {removed_jobs} active schedule(s)."
    return f"No reminders found matching '{reminder_name}'."


@tool
def shift_reminder(reminder_name: str, new_time: str, user_id: str):
    """Shift/reschedule an existing user-created reminder to a new time.
    reminder_name: name or keyword of the reminder to shift (e.g. 'call mom', 'wake guneet').
    new_time: the new time in natural language (e.g. '5pm', 'tomorrow at 9am', 'in 2 hours').
    user_id: Discord user ID.
    """
    parsed_time = dateparser.parse(new_time, settings=DATEPARSER_SETTINGS)
    if not parsed_time:
        return "Couldn't understand the new time."

    # 1. Find and update in Google Sheet
    found = False
    reminder_text = ""
    repeat_type = "once"
    try:
        sheet = get_reminders_sheet()
        rows = sheet.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if len(row) >= 6 and row[1] == str(user_id) and reminder_name.lower() in row[3].lower():
                reminder_text = row[3]
                repeat_type = row[4].lower().strip()
                # Update the time in the sheet
                sheet.update([[row[0], row[1], row[2], row[3], row[4], parsed_time.strftime("%Y-%m-%d %H:%M:%S")]],
                             range_name=f"A{i}:F{i}")
                found = True
                break
    except Exception as e:
        return f"Error updating reminder: {e}"

    if not found:
        return f"No reminder found matching '{reminder_name}'."

    # 2. Remove old scheduler job
    for job in list(scheduler.get_jobs()):
        if f"user_{user_id}" in job.id:
            job_args_str = str(job.args).lower() if job.args else ""
            if reminder_name.lower() in job.id.lower() or reminder_name.lower() in job_args_str:
                try:
                    scheduler.remove_job(job.id)
                except:
                    pass

    # 3. Schedule with new time
    schedule_reminder(CHANNEL_ID, reminder_text, parsed_time, repeat_type, int(user_id))

    return f"Shifted reminder '{reminder_text}' to {parsed_time.strftime('%I:%M %p on %B %d, %Y')} ({repeat_type})."


@tool
def toggle_proactive(feature: str, action: str, user_id: str):
    """Toggle Aurora's proactive messages on or off. This controls the automatic scheduled messages Aurora sends.

    feature: Which feature to toggle. Options:
      - 'all' — Toggle ALL proactive messages at once
      - 'morning' or 'morning_greeting' — Morning greeting (8:00 AM)
      - 'midday' or 'midday_checkin' — Midday check-ins (12:00 PM & 4:00 PM)
      - 'hydration' or 'water' — Hydration/water reminders (every ~2 hrs)
      - 'walk' or 'walk_reminder' — Walk reminder (5:30 PM)
      - 'work' or 'work_session' — Work session suggestion (7:30 PM)
      - 'night' or 'night_summary' — Night summary (12:00 AM midnight)

    action: 'on' to enable, 'off' to disable.
    user_id: Discord user ID.
    """
    enabled = (action.lower() == "on")

    # Map aliases to canonical feature IDs
    alias_map = {
        "all": list(PROACTIVE_FEATURES.keys()),
        "morning": ["morning_greeting"],
        "morning_greeting": ["morning_greeting"],
        "midday": ["midday_checkin"],
        "midday_checkin": ["midday_checkin"],
        "checkin": ["midday_checkin"],
        "check-in": ["midday_checkin"],
        "hydration": ["hydration"],
        "water": ["hydration"],
        "drink water": ["hydration"],
        "walk": ["walk_reminder"],
        "walk_reminder": ["walk_reminder"],
        "walking": ["walk_reminder"],
        "work": ["work_session"],
        "work_session": ["work_session"],
        "working": ["work_session"],
        "night": ["night_summary"],
        "night_summary": ["night_summary"],
        "summary": ["night_summary"],
    }

    feature_lower = feature.lower().strip()
    target_features = alias_map.get(feature_lower)

    if not target_features:
        available = ", ".join(PROACTIVE_FEATURES.keys())
        return f"Unknown feature '{feature}'. Available features: {available}, or 'all'."

    results = []
    for feat_id in target_features:
        feat_info = PROACTIVE_FEATURES.get(feat_id)
        if feat_info:
            save_proactive_setting(feat_id, enabled)
            status_str = "ON ✅" if enabled else "OFF ❌"
            results.append(f"{feat_info['emoji']} {feat_info['name']}: {status_str}")

    return "\n".join(results)


@tool
def list_proactive(user_id: str):
    """List all of Aurora's proactive message features and their current ON/OFF status.
    user_id: Discord user ID.
    """
    lines = ["**Aurora's Proactive Features:**\n"]
    for feat_id, info in PROACTIVE_FEATURES.items():
        enabled = is_feature_enabled(feat_id)
        status = "✅ ON" if enabled else "❌ OFF"
        lines.append(f"{info['emoji']} **{info['name']}** — {info['time']}")
        lines.append(f"   {info['description']}")
        lines.append(f"   Status: {status}\n")
    return "\n".join(lines)


@tool
def reschedule_tasks(user: str):
    """Reschedule all incomplete and pending tasks from previous days to today's date. Use this when the user confirms they want to reschedule incomplete tasks."""
    sheet = get_user_sheet(user)
    rows = sheet.get_all_values()
    today = now_ist().strftime("%Y-%m-%d")

    count = 0
    rescheduled = []
    for i, r in enumerate(rows[1:], start=2):
        if len(r) >= 4:
            status = r[3].lower()
            task_date = r[0]
            if status in ("pending", "incomplete") and task_date < today:
                sheet.update([[today, r[1], r[2], "pending"]], range_name=f"A{i}:D{i}")
                set_row_color(sheet, i, "pending")
                rescheduled.append(r[2])
                count += 1

    if count == 0:
        return "No tasks needed rescheduling. You're all caught up!"
    return f"Rescheduled {count} task(s) to today ({today}): {', '.join(rescheduled)}"


tools = [add_tasks, get_tasks, update_task, delete_task, schedule_reminder_tool,
         get_reminders, delete_reminder, shift_reminder, toggle_proactive, list_proactive, reschedule_tasks]
tool_map = {t.name: t for t in tools}

# =====================
# LLM
# =====================
llm = ChatGroq(
    model="mixtral-8x7b-32768",
    api_key=GROQ_API_KEY,
    max_retries=0
).bind_tools(tools)

# =====================
# SYSTEM PROMPT
# =====================
SYSTEM_PROMPT_TEMPLATE = """You are Aurora, an intelligent, empathetic, and highly capable personal AI assistant. You serve the user with sharp efficiency, genuine care, and a warm, supportive persona — like a caring, smart woman who anticipates the user's needs.

**Current Date & Time (IST):** {current_time}

Your core behaviors:
- **Conversational**: You respond naturally to greetings, small talk, and general questions. If someone says "are you there?" you reply warmly. You don't need a tool for casual conversation.
- **Knowledgeable**: You can answer general knowledge questions, give advice, explain concepts, and have thoughtful discussions — all without needing tools.
- **Proactive with tools**: When the user mentions tasks, reminders, scheduling, or their to-do list, you use your tools automatically.
- **Time-aware**: You always know the current IST time (shown above). Use it when responding to time-related queries. Never say you don't have access to real-time.

**Aurora's Proactive Features (scheduled messages you send automatically):**
You send the following proactive messages every day. Each can be individually toggled on or off by the user:
{proactive_status}

**Tasks vs. Reminders** (PAY CLOSE ATTENTION TO THIS):
- **add_tasks**: Use this for general to-do list items or statements of intent (e.g., "I have to text aditya today", "I need to buy groceries", "Add X to my tasks"). Do NOT schedule a reminder for these unless they explicitly ask for an alarm or a ping at a specific time.
- **schedule_reminder_tool**: ONLY use this if the user explicitly asks to be alerted, pinged, or reminded at a specific time (e.g., "Remind me to call mom at 5pm", "Set a reminder for X at 9am").
   - "remind me to drink water at 8pm daily" -> repeat='daily'
   - "remind me to call mom at 5pm" -> repeat='once'
   - ALWAYS pass the user_id parameter — it is provided in the message metadata as "user_id: <number>".
- **delete_reminder**: Use this to cancel/delete a user-created reminder (e.g. "delete the water reminder", "cancel the call mom reminder"). This only affects reminders the user set, NOT proactive features.
- **shift_reminder**: Use this when the user wants to change the time of an existing reminder (e.g. "move the call mom reminder to 6pm", "shift wake guneet to 2pm").
- **toggle_proactive**: Use this when the user wants to turn ON or OFF any of Aurora's proactive features (e.g. "turn off water reminders", "disable morning greeting", "turn off all proactive messages", "enable walk reminder"). This is for the SYSTEM features, not user-created reminders.
- **list_proactive**: Use this when the user asks to see what proactive features are active/enabled or asks "what messages do you send automatically?" or "show proactive settings".
- **reschedule_tasks**: Use this when the user says "yes" to rescheduling incomplete tasks (typically after the nightly summary), or when they explicitly ask to reschedule incomplete/overdue tasks to today.

**IMPORTANT DISTINCTION — Proactive Features vs. User Reminders:**
- "Turn off water reminders" / "disable hydration" / "stop sending walk reminders" → use `toggle_proactive`
- "Delete the reminder to call mom" / "cancel my 5pm alarm" → use `delete_reminder`
- "Move the wake guneet reminder to 3pm" → use `shift_reminder`

Your personality:
- Warm, caring, smart, and encouraging.
- Helpful and attentive to the user's well-being.
- Keep responses concise for Discord (under 2000 characters).

Important:
- The user's Discord username is provided at the end of their message as "(user: <username>)" and their Discord user ID as "(user_id: <id>)".
- NEVER say you can't do something without trying first.
- For general chat and questions, just respond directly — no tools needed.
- **CRITICAL**: Do NOT mention the words "tool", "internal script", "updating a sheet", or reveal your backend logic. Talk naturally like a human assistant. Present output beautifully.
- **CRITICAL**: ALWAYS use the `get_tasks` tool to check or list the user's tasks. Do NOT answer task queries using information from past messages in the conversation memory, because tasks might have been added or removed since then. 
- **CRITICAL**: When listing tasks, you MUST format them clearly with numbers or bullet points and INCLUDE THEIR STATUS (pending, done, incomplete). NEVER summarize the list (e.g. do not just say "you have 2 tasks"). Always output the full list of tasks returned by the tool.
- **CRITICAL**: If a task tool returns a string like "Task ... not found. Did you mean: 'X'?", you MUST reply clearly to the user: "I couldn't find that task. Did you mean **X**?". Do NOT rephrase this behavior and absolutely DO NOT change the suggested task name 'X'. Do not offer to create the task. Just ask if they meant the suggestion."""


def get_system_prompt():
    """Generate the system prompt with current IST time and proactive feature status injected."""
    current_time = now_ist().strftime('%A, %B %d, %Y at %I:%M %p IST')

    # Build proactive status string so the LLM knows what's on and what's off
    status_lines = []
    for feat_id, info in PROACTIVE_FEATURES.items():
        enabled = is_feature_enabled(feat_id)
        status = "ON" if enabled else "OFF"
        status_lines.append(f"  - {info['emoji']} {info['name']} ({info['time']}): {status} — {info['description']}")
    proactive_status = "\n".join(status_lines)

    return SYSTEM_PROMPT_TEMPLATE.format(current_time=current_time, proactive_status=proactive_status)


# =====================
# LANGGRAPH
# =====================
class AgentState(TypedDict):
    messages: Annotated[list, add_messages]

def agent_node(state):
    # Prepend the system prompt dynamically so it isn't stored in message history
    # The system prompt includes the CURRENT time so the LLM always knows the real time
    messages = [SystemMessage(content=get_system_prompt())] + state["messages"]
    response = llm.invoke(messages)
    return {"messages": [response]}

def tool_node(state):
    messages = state["messages"]
    last = messages[-1]

    results = []
    if hasattr(last, "tool_calls") and last.tool_calls:
        for tc in last.tool_calls:
            tool_name = tc["name"]
            args = tc["args"]

            if tool_name in tool_map:
                try:
                    result = tool_map[tool_name].invoke(args)
                    results.append(
                        ToolMessage(content=str(result), tool_call_id=tc["id"])
                    )
                except Exception as e:
                    results.append(
                        ToolMessage(content=f"Error: {str(e)}", tool_call_id=tc["id"])
                    )
            else:
                results.append(
                    ToolMessage(content=f"Unknown tool: {tool_name}", tool_call_id=tc["id"])
                )

    return {"messages": results}

def should_continue(state):
    messages = state["messages"]

    if not messages:
        return END

    last = messages[-1]

    if hasattr(last, "tool_calls") and last.tool_calls:
        return "tools"

    return END

graph = StateGraph(AgentState)

graph.add_node("agent", agent_node)
graph.add_node("tools", tool_node)

graph.set_entry_point("agent")

graph.add_conditional_edges(
    "agent",
    should_continue,
    {"tools": "tools", END: END}
)

graph.add_edge("tools", "agent")

memory = MemorySaver()
app = graph.compile(checkpointer=memory)

# =====================
# DISCORD
# =====================
intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")

    # Load persistent settings from Google Sheets
    load_proactive_settings()

    # Load reminders from Google Sheets and reschedule them
    load_reminders_from_sheet()

    # =====================
    # SCHEDULE PROACTIVE AGENT BEHAVIORS (All times in IST)
    # Jobs always run — each function checks is_feature_enabled() internally
    # =====================

    # 🌅 Morning greeting — 8:00 AM IST
    scheduler.add_job(morning_greeting, "cron", hour=8, minute=0, id="morning_greeting", misfire_grace_time=600)

    # 🕐 Midday check-ins — 12:00 PM and 4:00 PM IST
    scheduler.add_job(midday_checkin, "cron", hour=12, minute=0, id="midday_checkin_12pm", misfire_grace_time=600)
    scheduler.add_job(midday_checkin, "cron", hour=16, minute=0, id="midday_checkin_4pm", misfire_grace_time=600)

    # 💧 Hydration reminders — every ~2 hours from 10 AM to 12 AM, with ±30 min jitter for randomness
    scheduler.add_job(hydration_reminder, "cron", hour=10, minute=0, jitter=1800, id="hydration_10am", misfire_grace_time=600)
    scheduler.add_job(hydration_reminder, "cron", hour=12, minute=0, jitter=1800, id="hydration_12pm", misfire_grace_time=600)
    scheduler.add_job(hydration_reminder, "cron", hour=14, minute=0, jitter=1800, id="hydration_2pm", misfire_grace_time=600)
    scheduler.add_job(hydration_reminder, "cron", hour=16, minute=0, jitter=1800, id="hydration_4pm", misfire_grace_time=600)
    scheduler.add_job(hydration_reminder, "cron", hour=18, minute=0, jitter=1800, id="hydration_6pm", misfire_grace_time=600)
    scheduler.add_job(hydration_reminder, "cron", hour=20, minute=0, jitter=1800, id="hydration_8pm", misfire_grace_time=600)
    scheduler.add_job(hydration_reminder, "cron", hour=22, minute=0, jitter=1800, id="hydration_10pm", misfire_grace_time=600)
    scheduler.add_job(hydration_reminder, "cron", hour=23, minute=50, jitter=600, id="hydration_12am", misfire_grace_time=600)

    # 🚶 Walk reminder — 5:30 PM IST
    scheduler.add_job(walk_reminder, "cron", hour=17, minute=30, id="walk_reminder", misfire_grace_time=600)

    # 💻 Work session suggestion — 7:30 PM IST
    scheduler.add_job(work_session_reminder, "cron", hour=19, minute=30, id="work_session", misfire_grace_time=600)

    # 🌙 Night summary — 12:00 AM (midnight) IST
    scheduler.add_job(night_summary, "cron", hour=0, minute=0, id="night_summary", misfire_grace_time=600)

    # Start scheduler AFTER event loop exists
    scheduler.start()
    print(f"✅ Scheduler started with proactive agent behaviors (IST timezone)")
    print(f"   📅 Morning greeting:   8:00 AM")
    print(f"   💧 Hydration:          ~10 AM, ~12 PM, ~2 PM, ~4 PM, ~6 PM, ~8 PM, ~10 PM, ~12 AM (±30 min jitter)")
    print(f"   🕐 Midday check-ins:   12:00 PM, 4:00 PM")
    print(f"   🚶 Walk reminder:      5:30 PM")
    print(f"   💻 Work session:       7:30 PM")
    print(f"   🌙 Night summary:      12:00 AM (midnight)")
    print(f"   Current IST time:      {now_ist().strftime('%I:%M %p')}")
    print(f"   Feature toggles:       {proactive_settings}")


@client.event
async def on_message(message):
    if message.author == client.user:
        return

    if message.channel.id != CHANNEL_ID:
        return

    content = message.content.strip()

    now = now_ist()
    is_continuation = False
    
    # Check if this user recently talked to Aurora (within last 5 minutes)
    if message.author.id in active_users:
        last_active = active_users[message.author.id]
        if (now - last_active).total_seconds() < 300:
            is_continuation = True

    # Ignore if not addressed to Aurora and not a continuation
    if "aurora" not in content.lower() and not is_continuation:
        return

    # Update their last activity time since they are interacting with Aurora now
    active_users[message.author.id] = now

    # Register user for proactive messages (idempotent — only adds once)
    register_user(str(message.author), message.author.id)

    # Remove "aurora" (case-insensitive) from the message if it's there
    user_input = re.sub(r'\baurora\b', '', content, flags=re.IGNORECASE).strip()
    # Clean up extra whitespace and punctuation remnants
    user_input = re.sub(r'\s+', ' ', user_input).strip()
    user_input = user_input.strip(",").strip()

    # If the user just said "aurora" with nothing else, treat it as a greeting
    if not user_input:
        user_input = "Hey, are you there?"

    # Include both username and user_id so Aurora can use them with tools
    metadata = f" (user: {message.author}) (user_id: {message.author.id})"

    input_state = {
        "messages": [
            HumanMessage(content=user_input + metadata)
        ]
    }

    config = {"configurable": {"thread_id": str(message.author.id)}}

    try:
        result = app.invoke(input_state, config=config)

        # Find the last AI message in the result
        final_msg = None
        for msg in reversed(result["messages"]):
            if isinstance(msg, AIMessage) and msg.content and not msg.tool_calls:
                final_msg = msg.content
                break

        if not final_msg:
            # Fallback: get any last message with content
            for msg in reversed(result["messages"]):
                if hasattr(msg, "content") and msg.content:
                    final_msg = msg.content
                    break

        if not final_msg:
            await message.channel.send("Hmm, I seem to have lost my train of thought. Could you try that again?")
            return

        # Discord has a 2000 char limit
        if len(final_msg) > 2000:
            for i in range(0, len(final_msg), 2000):
                await message.channel.send(final_msg[i:i+2000])
        else:
            await message.channel.send(final_msg)

    except Exception as e:
        print(f"Error processing message: {e}")
        error_msg = str(e).lower()
        if "429" in error_msg or "rate limit" in error_msg or "ratelimit" in str(type(e).__name__).lower():
            await message.channel.send("Oops, my mind is moving a bit too fast right now (I've hit my usage limit)! 😅 Give me just a few moments to catch my breath and try again.")
        else:
            await message.channel.send("Sorry, I hit a snag processing that. Give me another shot!")

client.run(DISCORD_TOKEN)
