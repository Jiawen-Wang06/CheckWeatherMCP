# CheckWeatherMCP

CheckWeatherMCP is a command-line weather assistant. It uses an OpenAI chat model to interpret a question, calls a local Model Context Protocol (MCP) weather tool, and fetches current conditions from OpenWeatherMap. Results include the weather description, temperature and feels-like temperature in Celsius, humidity, and wind speed in meters per second.

## How it works

- [`mcp_client.py`](./mcp_client.py) starts an interactive chat and connects to the weather server over MCP's standard I/O transport. LangGraph routes weather-tool calls, and the client uses the `gpt-4o-mini` OpenAI model.
- [`weather_server.py`](./weather_server.py) exposes the `get_weather` tool and makes requests to OpenWeatherMap.
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

## Troubleshooting

- If the OpenAI API key is missing or invalid, check `OPENAI_API_KEY` in `.env`.
- If the weather tool reports an authentication error, verify the `OPENWEATHERMAP_API_KEY` value and that the key is active.
- If a city cannot be found, try including its country code, for example `London,uk`.
