import discord
import os
import re
from difflib import get_close_matches
from dotenv import load_dotenv
from datetime import datetime
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
# GOOGLE SHEETS
# =====================
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

import json

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

# =====================
# REMINDER SYSTEM
# =====================
scheduler = AsyncIOScheduler()

async def send_reminder(channel_id, text, user_id, reminder_type="once"):
    """Send a reminder and tag the user. Auto-remove one-time reminders from the sheet."""
    channel = client.get_channel(channel_id)
    if channel:
        mention = f"<@{user_id}>"
        await channel.send(f"⏰ {mention} **Reminder:** {text}")

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
    """Schedule a reminder with APScheduler. Supports once, daily, weekly."""
    if repeat == "daily":
        scheduler.add_job(
            send_reminder,
            "cron",
            hour=run_time.hour,
            minute=run_time.minute,
            args=[channel_id, text, user_id, "daily"]
        )
    elif repeat == "weekly":
        scheduler.add_job(
            send_reminder,
            "cron",
            day_of_week=run_time.strftime("%a").lower()[:3],
            hour=run_time.hour,
            minute=run_time.minute,
            args=[channel_id, text, user_id, "weekly"]
        )
    else:
        # One-time reminder
        scheduler.add_job(
            send_reminder,
            "date",
            run_date=run_time,
            args=[channel_id, text, user_id, "once"]
        )

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
            parsed_time = dateparser.parse(time_str)

            if not parsed_time:
                continue

            rtype_lower = rtype.lower().strip()

            # For one-time reminders, only schedule if they're in the future
            if rtype_lower == "once":
                if parsed_time < datetime.now():
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
    date = datetime.now().strftime("%Y-%m-%d")
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

    today = datetime.now().strftime("%Y-%m-%d")
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
    parsed_time = dateparser.parse(time)

    if not parsed_time:
        return "Couldn't understand the time."

    # Save to Google Sheets
    try:
        sheet = get_reminders_sheet()
        sheet.append_row([
            user,
            str(user_id),
            datetime.now().strftime("%Y-%m-%d"),
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

tools = [add_tasks, get_tasks, update_task, delete_task, schedule_reminder_tool, get_reminders]
tool_map = {t.name: t for t in tools}

# =====================
# LLM
# =====================
llm = ChatGroq(
    model="llama-3.1-8b-instant",
    api_key=GROQ_API_KEY,
    max_retries=0
).bind_tools(tools)

# =====================
# SYSTEM PROMPT
# =====================
SYSTEM_PROMPT = """You are Aurora, an intelligent, empathetic, and highly capable personal AI assistant. You serve the user with sharp efficiency, genuine care, and a warm, supportive persona — like a caring, smart woman who anticipates the user's needs.

Your core behaviors:
- **Conversational**: You respond naturally to greetings, small talk, and general questions. If someone says "are you there?" you reply warmly. You don't need a tool for casual conversation.
- **Knowledgeable**: You can answer general knowledge questions, give advice, explain concepts, and have thoughtful discussions — all without needing tools.
- **Proactive with tools**: When the user mentions tasks, reminders, scheduling, or their to-do list, you use your tools automatically.

**Tasks vs. Reminders** (PAY CLOSE ATTENTION TO THIS):
- **add_tasks**: Use this for general to-do list items or statements of intent (e.g., "I have to text aditya today", "I need to buy groceries", "Add X to my tasks"). Do NOT schedule a reminder for these unless they explicitly ask for an alarm or a ping at a specific time.
- **schedule_reminder_tool**: ONLY use this if the user explicitly asks to be alerted, pinged, or reminded at a specific time (e.g., "Remind me to call mom at 5pm", "Set a reminder for X at 9am").
   - "remind me to drink water at 8pm daily" -> repeat='daily'
   - "remind me to call mom at 5pm" -> repeat='once'
   - ALWAYS pass the user_id parameter — it is provided in the message metadata as "user_id: <number>".

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
- **CRITICAL**: If a task tool returns a string like "Task ... not found. Did you mean: 'X'?", you MUST reply clearly to the user: "I couldn't find that task. Did you mean **X**?". Do NOT rephrase this behavior and absolutely DO NOT change the suggested task name 'X'. Do not offer to create the task. Just ask if they meant the suggestion."""

# =====================
# LANGGRAPH
# =====================
class AgentState(TypedDict):
    messages: Annotated[list, add_messages]

def agent_node(state):
    # Prepend the system prompt dynamically so it isn't stored in message history
    messages = [SystemMessage(content=SYSTEM_PROMPT)] + state["messages"]
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
    # Load reminders from Google Sheets and reschedule them
    load_reminders_from_sheet()
    # Start scheduler AFTER event loop exists
    scheduler.start()
    print("Scheduler started. Reminders loaded.")

# Track last interaction to allow conversation continuation without saying "aurora" every time
active_users = {}

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    if message.channel.id != CHANNEL_ID:
        return

    content = message.content.strip()

    now = datetime.now()
    is_continuation = False
    
    # Check if this user recently talked to Aurora (within last 5 minutes)
    if message.author.id in active_users:
        if (now - active_users[message.author.id]).total_seconds() < 300:
            is_continuation = True

    # Ignore if not addressed to Aurora and not a continuation
    if "aurora" not in content.lower() and not is_continuation:
        return

    # Update their last activity time since they are interacting with Aurora now
    active_users[message.author.id] = now

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
