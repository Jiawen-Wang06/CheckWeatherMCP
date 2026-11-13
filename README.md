# CheckWeatherMCP

CheckWeatherMCP is a command-line weather assistant. It uses an OpenAI chat model to interpret a question, calls a local Model Context Protocol (MCP) weather tool, and fetches current conditions from OpenWeatherMap. Results include the weather description, temperature and feels-like temperature in Celsius, humidity, and wind speed in meters per second.

## How it works

- [`mcp_client.py`](./mcp_client.py) starts an interactive chat and connects to the weather server over MCP's standard I/O transport. LangGraph routes weather-tool calls, and the client uses the `gpt-4o-mini` OpenAI model. It also lists and invokes prompts provided by the server.
- [`weather_server.py`](./weather_server.py) exposes the `get_weather` tool and makes requests to OpenWeatherMap. It also provides `compare_weather_prompt`, which guides the assistant to fetch and compare two locations' temperatures, conditions, and wind speeds in a concise structured summary.
- Both files load credentials from the project-root `.env` file. `.env` is ignored by Git; use [`.env.example`](./.env.example) as a template.

## Setup

1. Install Python and the project dependencies in your Python environment:

   ```powershell
   python -m pip install mcp requests python-dotenv langgraph langchain-openai langchain-core langchain-mcp-adapters typing-extensions
   ```

2. Ensure a `.env` file exists in the project root. To create one from the template without replacing an existing file, run:

   ```powershell
   if (-not (Test-Path .env)) { Copy-Item .env.example .env }
   ```

   Fill in both values in `.env`:

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

The client starts the MCP weather server automatically. Ask a question such as:

```text
What's the weather in London, UK?
```

Enter `exit`, `quit`, or `q` to end the chat. The server can also be run on its own with `python .\weather_server.py`, but the client is needed to chat with the assistant.

### Load a server resource

List the available resources, then load one by its URI:

```text
/resources
/resource file://delivery_log
```

The current delivery-log resource reads `delivery_log.txt` from the project root. After loading it, enter an instruction to ask the assistant to work with the content, or press Enter to add it to the ongoing chat context. Resource content is sent to the OpenAI model when passed to the assistant; avoid loading sensitive data unless you are authorized to share it with that service.

### Compare two locations with the server prompt

In the chat, list the prompts available from the server:

```text
/prompts
```

Invoke the comparison prompt with two locations:

```text
/prompt compare_weather_prompt "London,uk" "Paris,fr"
```

The client sends the prompt returned by the server to the assistant. The assistant uses the weather tool to gather both locations' data and presents their key differences, typically in a table or bulleted list. For a prompt argument containing spaces, keep it inside quotes.

## Troubleshooting

- If the OpenAI API key is missing or invalid, check `OPENAI_API_KEY` in `.env`.
- If the weather tool reports an authentication error, verify the `OPENWEATHERMAP_API_KEY` value and that the key is active.
- If a city cannot be found, try including its country code, for example `London,uk`.
