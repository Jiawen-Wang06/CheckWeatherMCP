# CheckWeatherMCP

CheckWeatherMCP is a multi-server MCP client and assistant that combines weather data with task management. The client connects to multiple MCP servers over stdio, loads the tools from each server into a single LangGraph agent, and lets the assistant call the relevant tool set based on the user's request.

## What it does

- [`mcp_client.py`](./mcp_client.py) creates a `MultiServerMCPClient` and connects to the configured servers in a `server_configs` dictionary.
- The agent loads tools from all connected servers and uses `gpt-4o-mini` to decide when to call them.
- The project currently includes:
  - [`weather_server.py`](./weather_server.py): exposes weather tools and resources for OpenWeatherMap data.
  - [`task_server.py`](./task_server.py): exposes task-management tools, a travel-planning prompt, and a meeting-notes resource.
- Both servers load secrets from the project-root `.env` file. `.env` is ignored by Git; use [`.env.example`](./.env.example) as a template.

## Architecture

The client connects to two MCP servers at startup:

```python
server_configs = {
    "weather": {
        "command": "python",
        "args": ["weather_server.py"],
        "transport": "stdio",
    },
    "tasks": {
        "command": "python",
        "args": ["task_server.py"],
        "transport": "stdio",
    }
}
```

This means the agent can combine capabilities from both servers without requiring two separate chats. A single conversation can:

- fetch the current weather in a city,
- add items to the task list,
- list tasks,
- use server prompts tailored to the relevant server, and
- load and work with server resources.

## Setup

1. Install Python and the project dependencies in your Python environment:

   ```powershell
   python -m pip install mcp requests python-dotenv langgraph langchain-openai langchain-core langchain-mcp-adapters typing-extensions
   ```

2. Ensure a `.env` file exists in the project root. To create one from the template without replacing an existing file, run:

   ```powershell
   if (-not (Test-Path .env)) { Copy-Item .env.example .env }
   ```

3. Fill in the required values in `.env`:

   ```dotenv
   OPENWEATHERMAP_API_KEY=your_openweathermap_api_key
   OPENAI_API_KEY=your_openai_api_key
   ```

   Get an API key from [OpenWeatherMap](https://openweathermap.org/api) and an API key for the OpenAI API. Keep `.env` private and never commit it.

## Run

From the project root, start the client:

```powershell
python .\mcp_client.py
```

The client starts the configured MCP servers and loads their tools automatically. You can then ask questions such as:

```text
What's the weather in London, UK?
```

or:

```text
Add "Buy groceries" to my task list.
```

Enter `exit`, `quit`, or `q` to end the chat.

## Chat commands

The client supports server-aware commands for listing and invoking capabilities from any connected server.

### List all prompts

```text
/prompts
```

This lists prompts from each connected server, with the server name included in the output.

### Invoke a prompt on a specific server

```text
/prompt weather compare_weather_prompt "London,uk" "Paris,fr"
```

```text
/prompt tasks plan_trip_prompt "Tokyo" 4
```

The format is:

```text
/prompt <server_name> <prompt_name> "arg1" "arg2" ...
```

For prompt arguments containing spaces, keep them inside quotes.

### List all resources

```text
/resources
```

This lists the resources available on each server, including each resource URI.

### Load a resource from a specific server

```text
/resource weather file://delivery_log
```

```text
/resource tasks file://meeting_notes
```

The resource content is loaded and then either:

- added to the conversation context immediately when you press Enter, or
- used as the basis for a follow-up instruction after you type a task for the assistant.

## Server capabilities

### Weather server

[`weather_server.py`](./weather_server.py) exposes:

- `get_weather(location: str)`
- `compare_weather_prompt(location_a: str, location_b: str)`
- `file://delivery_log` resource

This server fetches weather data from OpenWeatherMap and returns the location, conditions, temperature, feels-like temperature, humidity, and wind speed.

### Task server

[`task_server.py`](./task_server.py) exposes:

- `add_task(task_description: str)`
- `list_tasks()`
- `plan_trip_prompt(destination: str, duration_in_days: int)`
- `file://meeting_notes` resource

This server stores tasks in `tasks.txt` and provides a travel-planning workflow that saves itinerary items into the task list.

## Troubleshooting

- If the OpenAI API key is missing or invalid, check `OPENAI_API_KEY` in `.env`.
- If the weather tool reports an authentication error, verify the `OPENWEATHERMAP_API_KEY` value and that the key is active.
- If a city cannot be found, try including its country code, for example `London,uk`.
- If a prompt or resource is not found, check the exact server name and the URI or prompt name you are using.
- If a tool call fails, make sure both MCP server processes are running through the client startup path and that the server files are in the project root.
