import os
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from typing import List
from pathlib import Path
from datetime import date, timedelta

# Define the file where tasks will be stored (override with TASKS_FILE env var, e.g. for evals)
TASKS_FILE = os.getenv("TASKS_FILE", "tasks.txt")

# Initialize the FastMCP server with a descriptive name
# Quiet by default; set MCP_LOG_LEVEL=INFO to see "Processing request of type ..." lines.
mcp = FastMCP("TaskManagementAssistant", log_level=os.getenv("MCP_LOG_LEVEL", "WARNING").upper())

@mcp.tool()
def add_task(task_description: str) -> str:
    """
    Adds a new task to the persistent task list file.

    This tool will create the task file if it doesn't exist. It appends
    the new task to the end of the file, ensuring each task is on a new line.

    Args:
        task_description: A string describing the task to be added.
                          For example: "Buy groceries" or "Finish the report".

    Returns:
        A string confirming that the task was successfully added.
    """
    try:
        # 'a' mode will append to the file, and create it if it doesn't exist
        with open(TASKS_FILE, "a", encoding="utf-8") as f:
            f.write(f"{task_description}\n")
        return f"Task '{task_description}' was added successfully."
    except Exception as e:
        return f"An error occurred while adding the task: {e}"

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_tasks() -> List[str]:
    """
    Lists all the tasks from the persistent task list file.

    This tool reads all tasks from the file. If the file does not exist or
    is empty, it returns an empty list.

    Returns:
        A list of strings, where each string is a task. Returns an empty
        list if no tasks are found.
    """
    if not os.path.exists(TASKS_FILE):
        # Return an empty list if the file doesn't exist, as there are no tasks
        # The LLM can interpret this as "no tasks to show"
        return []

    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as f:
            # Read all lines, and strip leading/trailing whitespace (like newlines)
            tasks = [line.strip() for line in f.readlines()]
            # Filter out any empty lines that might have been created
            return [task for task in tasks if task]
    except Exception as e:
        # In case of an error, we can return a list with an error message,
        # but for simplicity and better type consistency, we'll return an empty list.
        # The LLM can be prompted to handle this gracefully
        print(f"Error reading tasks file: {e}")
        return []

@mcp.prompt()
def plan_trip_prompt(destination: str, duration_in_days: int) -> str:
    """
    Creates a weather-aware travel itinerary for a given destination and duration, then saves it as a series of tasks in the user's to-do list.
    This is the best prompt to use when a user asks to plan a trip, create an itinerary, or asks for travel suggestions.

    Args:
        destination: The city for the trip (e.g., "Montreal", "Banff").
        duration_in_days: The number of days for the trip (e.g., 3).
    """
    return f"""
    You are an expert travel consultant. Your goal is to help the user by generating a weather-aware travel itinerary and saving it to their task list for later reference.

    The user wants a plan for a {duration_in_days}-day trip to {destination}. Assume the trip starts tomorrow unless the user has said otherwise.

    Follow these steps carefully:
    1.  First, check the weather forecast for {destination} with the weather forecast tool, for the {duration_in_days} trip days starting tomorrow (Day 1 is the day after today). Forecasts only cover about the next 5 days.
    2.  Using the forecast and your general knowledge, brainstorm a simple, day-by-day itinerary with one or two key attractions or activities per day. Put outdoor activities on dry days and indoor activities (museums, galleries, food markets, etc.) on days with a high chance of rain (50% or more) or noticeable rain, and describe each day's rain chance accurately (70% is "rain likely", not "light rain possible"). If some trip days are beyond the forecast range, say that the weather for those days is not known yet.
    3.  After you have formulated the plan, you MUST perform a critical action: for each individual activity or attraction in your suggested itinerary, save it to the user's task list, in itinerary order. Start each task with the day and a short weather note, for example "Day 1 (Thu, rain likely): Visit the city's main art museum".
    4.  Once all the itinerary items have been added as tasks, present a friendly confirmation message to the user: summarize the expected weather for each day, explain how it shaped the plan, and tell them the plan has been saved to their to-do list.
    """

MAX_TRIP_DAYS = 15  # the forecast tool accepts dates up to 14 days ahead, so a trip can span at most 15 dates


@mcp.prompt()
def plan_trip_for_future_date(destination: str, from_date: str, to_date: str) -> str:
    """
    Plans a trip for specific dates using the weather forecast for those dates, then saves each activity to the user's to-do list.
    Use this when the user gives exact travel dates (up to 14 days from today).

    Args:
        destination: The city for the trip (e.g., "Quebec City").
        from_date: First day of the trip, YYYY-MM-DD (e.g., "2026-03-05").
        to_date: Last day of the trip, YYYY-MM-DD (e.g., "2026-03-07").
    """
    try:
        start, end = date.fromisoformat(from_date.strip()), date.fromisoformat(to_date.strip())
    except ValueError:
        raise ValueError("from_date and to_date must use the format YYYY-MM-DD, for example 2026-03-05.")
    if end < start:
        raise ValueError(f"to_date ({end}) must be on or after from_date ({start}).")
    trip_days = (end - start).days + 1
    if trip_days > MAX_TRIP_DAYS:
        raise ValueError(f"The trip is {trip_days} days long; the maximum is {MAX_TRIP_DAYS} days.")

    # Weekdays are computed here: models often get them wrong when working them out themselves.
    trip_dates = "\n".join(f"        - {start + timedelta(days=i)} ({(start + timedelta(days=i)):%A})" for i in range(trip_days))

    return f"""
    You are an expert travel consultant. The user is planning a trip to {destination} from {start} ({start:%A}) to {end} ({end:%A}), {trip_days} day(s) in total.
    Your goal is to plan the trip around the weather forecast and save it to the user's to-do list.

    The trip dates (use exactly these dates and weekdays):
{trip_dates}

    Follow these steps carefully:
    1.  Call the weather forecast tool for {destination} with from_date="{start}" and to_date="{end}".
        If the tool returns an error (for example, dates in the past or more than 14 days ahead), explain the problem to the user and stop. Do not add any tasks.
    2.  Plan one or two specific, real activities for EVERY trip date. Put outdoor activities on dry days and indoor activities (museums, galleries, food markets, etc.) on days with a high chance of rain (50% or more) or noticeable rain.
        Dates listed in "unavailable_dates" don't have a forecast yet. Still plan specific, real activities for them, choosing ones that suit the typical weather for that place and season, and say clearly that their forecast is not available yet.
        Every task must name a specific place or activity. Never save placeholder tasks such as "Plan activities based on weather".
    3.  You MUST save each activity to the user's task list as a separate task, in date order. Start each task with the date, weekday and a short weather note, for example "{start} ({start:%a}, rain likely): Visit the city's main art museum". For dates without a forecast, use "forecast not available yet" as the weather note and still name a specific activity, for example "{end} ({end:%a}, forecast not available yet): Take a guided walking tour of the old town".
    4.  Finally, give the user a short summary: the expected weather for each day, how it shaped the plan, and confirmation that every activity was saved to their to-do list.
    """


@mcp.resource("file://meeting_notes")
def meeting_notes_resource() -> list[str]:
    """
    Reads a meeting notes file and returns its contents as a list of lines.
    The notes contain discussion points and action items for an Ed Tech company.
    """
    try:
        notes_file = Path("meeting_notes.txt")
        if not notes_file.exists():
            return ["Error: The meeting_notes.txt file was not found on the server."]

        # Read the file, remove leading/trailing whitespace, and split into lines
        return notes_file.read_text(encoding="utf-8").strip().splitlines()

    except Exception as e:
        return [f"An unexpected error occurred while reading the meeting notes: {str(e)}"]

if __name__ == "__main__":
    mcp.run(transport="stdio")