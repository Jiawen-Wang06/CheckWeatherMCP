import asyncio
import os
import shlex
import uuid
from datetime import date
import logging
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pathlib import Path
from dotenv import load_dotenv
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import AnyMessage, add_messages
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import tools_condition
from typing import Annotated, List, Literal
from requests import session
from typing_extensions import TypedDict
from langchain_openai import ChatOpenAI
from langchain_core.messages import ToolMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_mcp_adapters.tools import load_mcp_tools
from langchain_mcp_adapters.client import MultiServerMCPClient

# Quiet by default; set LOG_LEVEL=INFO (or DEBUG) to see HTTP requests and other library logs.
logging.basicConfig(level=os.getenv("LOG_LEVEL", "WARNING").upper())
load_dotenv(Path(__file__).with_name(".env"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
chat_config = {"configurable": {"thread_id": "weather-session-chat"}}
prompt_config = {"configurable": {"thread_id": f"weather-session-prompt-{uuid.uuid4()}"}}

# --- Multi-server configuration dictionary ---
# This dictionary defines all the servers the client will connect to
server_configs = {
    "weather": {
        "command": "python",
        "args": ["weather_server.py"],
        "transport": "stdio",
        "env": {"MCP_LOG_LEVEL": os.getenv("MCP_LOG_LEVEL", "WARNING")},
    },
    "tasks": {
        "command": "python",
        "args": ["task_server.py"],
        "transport": "stdio",
        "env": {"MCP_LOG_LEVEL": os.getenv("MCP_LOG_LEVEL", "WARNING")},
    }
}

# LangGraph state definition
class State(TypedDict):
    messages: Annotated[List[AnyMessage], add_messages]


DEFERRED_WRITE = ("NOT EXECUTED: this change was requested in the same step as lookups, before their results were "
                  "known. Nothing was changed. Check the lookup results first, then call it again only if it is still needed.")


def make_ordered_tool_node(tools: list):
    """
    Execute the model's tool calls safely.

    LangGraph's ToolNode runs all tool calls from one model turn concurrently. That's fine for
    reads, but for writes it loses ordering: a 3-day itinerary saved as six add_task calls landed
    in tasks.txt in random order, and "add X then list" could list before the add finished
    (found by the eval's `order` check). Servers mark side-effect-free tools with the standard MCP
    `readOnlyHint` annotation; a batch runs concurrently only if every call in it is read-only,
    otherwise calls run one by one in the order the model issued them.
    """
    tools_by_name = {t.name: t for t in tools}
    read_only = {t.name for t in tools if (t.metadata or {}).get("readOnlyHint")}

    async def run_one(call: dict) -> ToolMessage:
        tool = tools_by_name.get(call["name"])
        if tool is None:
            return ToolMessage(content=f"Error: '{call['name']}' is not a valid tool. Use one of: {', '.join(tools_by_name)}.",
                               tool_call_id=call["id"], name=call["name"], status="error")
        try:
            return await tool.ainvoke(call)  # invoking with a ToolCall returns a ToolMessage
        except Exception as e:  # surface tool failures to the model instead of crashing the graph
            return ToolMessage(content=f"Error: {e!r}", tool_call_id=call["id"], name=call["name"], status="error")

    async def ordered_tool_node(state: State) -> dict:
        calls = state["messages"][-1].tool_calls
        reads = [c for c in calls if c["name"] in read_only]
        if len(reads) == len(calls):
            return {"messages": list(await asyncio.gather(*(run_one(c) for c in calls)))}
        if reads:
            # "Look before you write": the model asked for lookups AND changes in the same step, i.e. before it
            # could see the lookup results (e.g. get_weather + add_task("pack umbrella") for "add it IF it's
            # raining"). Run the lookups and hold the changes back, so the model decides again with the data.
            done = dict(zip((c["id"] for c in reads), await asyncio.gather(*(run_one(c) for c in reads))))
            return {"messages": [done.get(c["id"]) or ToolMessage(content=DEFERRED_WRITE, tool_call_id=c["id"],
                                                                  name=c["name"], status="error")
                                 for c in calls]}
        return {"messages": [await run_one(c) for c in calls]}

    return ordered_tool_node


def create_graph(tools: list, model: str = "gpt-4o-mini", api_key: str | None = None, checkpointer=None,
                 today: date | None = None):
    """
    api_key: OpenAI key to use (defaults to OPENAI_API_KEY from .env). The web app passes each
             visitor's own key here; it lives only in this graph object, which is built per request.
    checkpointer: conversation memory; the web app shares one across requests so history survives.
    today: the date the agent treats as today (default: the real date, read on every turn). The eval pins
           it to the fixtures' date so "tomorrow" or "March 5" resolve the same way on every run.
    """
    # LLM configuration
    llm = ChatOpenAI(model=model, temperature=0, openai_api_key=api_key or OPENAI_API_KEY)
    llm_with_tools = llm.bind_tools(tools)
    prompt_template = ChatPromptTemplate.from_messages([
        ("system", "You are a helpful assistant. You have access to tools for checking the weather (current conditions and "
                   "forecasts) and managing a to-do list. Use the tools when necessary based on the user's request. "
                   "Today's date is {today}. Use it to turn relative dates such as 'tomorrow' or 'next Friday' into exact dates. "
                   "Never answer weather questions from memory: always get the data with the weather tools. "
                   "If the user names a place, call the tools right away with your best reading of it (fix obvious typos, "
                   "use the best-known place with that name); only ask for clarification if a tool returns an error. "
                   "If the user names no place at all, ask which city they mean instead of guessing. "
                   "When an action depends on a tool result (for example 'add a task if it's raining'), get that result "
                   "first, then decide."),
        MessagesPlaceholder("messages")
    ]).partial(today=lambda: (today or date.today()).strftime("%A, %Y-%m-%d"))
    chat_llm = prompt_template | llm_with_tools

    # Define chat node
    async def chat_node(state: State) -> dict:
        return {"messages": await chat_llm.ainvoke({"messages": state["messages"]})}

    # Build LangGraph with tool routing
    graph = StateGraph(State)
    graph.add_node("chat_node", chat_node)
    graph.add_node("tool_node", make_ordered_tool_node(tools))
    graph.add_edge(START, "chat_node")
    graph.add_conditional_edges("chat_node", tools_condition, {
        "tools": "tool_node",
        "__end__": END
    })
    graph.add_edge("tool_node", "chat_node")

    return graph.compile(checkpointer=checkpointer if checkpointer is not None else MemorySaver())

async def list_all_prompts(client: MultiServerMCPClient, server_configs: dict):
    print("\nAvailable Prompts from all servers:")
    print("-----------------------------------")
    
    any_prompts_found = False
    
    # Iterate through the names of the servers defined in your server_configs
    for server_name in server_configs.keys():
        try:
            # Opening a session for a specific server to interact with its prompts
            async with client.session(server_name) as session:
                prompt_response = await session.list_prompts()

                if prompt_response and prompt_response.prompts:
                    any_prompts_found = True
                    # Print a header for the server to group the prompts
                    print(f"\n--- Server: '{server_name}' ---")
                    for p in prompt_response.prompts:
                        print(f"  Prompt: {p.name}")
                        if p.arguments:
                            arg_list = [f"{arg.name}" for arg in p.arguments]
                            print(f"    Arguments: {', '.join(arg_list)}")
                        else:
                            print("    Arguments: None")
        except Exception as e:
            print(f"\nCould not fetch prompts from server '{server_name}': {e}")
    
    print("\nUse: /prompt <server_name> <prompt_name> \"arg1\" \"arg2\" ...")
    print("-----------------------------------")
    if not any_prompts_found:
        print("\nNo prompts were found on any connected servers.")

async def handle_prompt_invocation(client: MultiServerMCPClient, command: str) -> str | None: 
    try:
        # Use shlex to correctly parse arguments, even with spaces
        parts = shlex.split(command.strip())
        
        # Command should be: /prompt <server_name> <prompt_name> [args...]
        if len(parts) < 3:
            print("\nUsage: /prompt <server_name> <prompt_name> \"arg1\" \"arg2\" ...")
            return None

        server_name = parts[1]
        prompt_name = parts[2]
        user_args = parts[3:]

        # --- Validate the prompt and arguments against the specific server ---
        prompt_def = None
        async with client.session(server_name) as session:
            all_prompts_resp = await session.list_prompts()
            if all_prompts_resp and all_prompts_resp.prompts:
                # Find the matching prompt definition
                prompt_def = next((p for p in all_prompts_resp.prompts if p.name == prompt_name), None)

        if not prompt_def:
            print(f"\nError: Prompt '{prompt_name}' not found on server '{server_name}'.")
            return None

        # Check if the number of user-provided arguments matches what the prompt expects
        if len(user_args) != len(prompt_def.arguments):
            expected_args = [arg.name for arg in prompt_def.arguments]
            print(f"\nError: Invalid number of arguments for prompt '{prompt_name}'.")
            print(f"Expected {len(expected_args)} arguments: {', '.join(expected_args)}")
            return
        
        # --- Fetch and execute the prompt ---
        arg_dict = {arg.name: val for arg, val in zip(prompt_def.arguments, user_args)}

        # Use the client's get_prompt method, specifying server, prompt, and args
        prompt_messages = await client.get_prompt(
            server_name=server_name, 
            prompt_name=prompt_name, 
            arguments=arg_dict
        )
        
        # The prompt text is the content of the first message returned
        prompt_text = prompt_messages[0].content

        print("\n--- Prompt loaded successfully. Preparing to execute... ---")
        
        # Return the fetched text instead of calling the agent
        return prompt_text

    except Exception as e:
        print(f"\nAn error occurred during prompt invocation: {e}")

async def list_all_resources(client: MultiServerMCPClient, server_configs: dict):
    print("\nAvailable Resources from all servers:")
    print("-------------------------------------")
    
    any_resources_found = False
    
    # Iterate through the names of the servers defined in your server_configs
    for server_name in server_configs.keys():
        try:
            # Opening a session for a specific server to list its resources
            async with client.session(server_name) as session:
                # The method to list resources is session.list_resources()
                resource_response = await session.list_resources()

                if resource_response and resource_response.resources:
                    any_resources_found = True
                    print(f"\n--- Server: '{server_name}' ---")
                    for r in resource_response.resources:
                        # The most important identifier for a resource is its URI
                        print(f"  Resource URI: {r.uri}")
                        if r.description:
                            print(f"    Description: {r.description}")
        except Exception as e:
            print(f"\nCould not fetch resources from server '{server_name}': {e}")
    
    print("\nUse: /resource <server_name> <resource_uri>")
    print("-----------------------------------")        
    
    if not any_resources_found:
        print("\nNo resources were found on any connected servers.")

async def handle_resource_invocation(client: MultiServerMCPClient, command: str) -> str | None:
    try:
        parts = command.strip().split()
        if len(parts) != 3:
            print("\nUsage: /resource <server_name> <resource_uri>")
            return None

        server_name = parts[1]
        resource_uri = parts[2]

        print(f"\n--- Fetching resource '{resource_uri}' from server '{server_name}'... ---")

        # It requires the server_name and the URI of the resource
        
        blobs = await client.get_resources(server_name=server_name, uris=[resource_uri])

        if not blobs:
            print("Error: Resource not found or content is empty.")
            return None

        # Converting LangChain Blobs to string content
        
        resource_content = blobs[0].as_string()
        
        print("--- Resource content loaded successfully. ---")
        return resource_content

    except Exception as e:
        print(f"\nAn error occurred while fetching the resource: {e}")
        return None

async def prepare_agent_request(
    client, user_input: str
) -> tuple[str, Literal["chat", "prompt"]] | None:
    if user_input.lower() == "/prompts":
        await list_all_prompts(client, server_configs)
        return None

    if user_input.lower() == "/resources":
        await list_all_resources(client, server_configs)
        return None

    command = user_input.split(maxsplit=1)[0].lower() if user_input else ""

    if command == "/prompt":
        prompt_text = await handle_prompt_invocation(client, user_input)
        return (prompt_text, "prompt") if prompt_text else None

    if command == "/resource":
        resource_content = await handle_resource_invocation(client, user_input)
        if not resource_content:
            return None

        action_prompt = input(
            "Resource loaded. What should I do with this content? "
            "(Press Enter to add it to conversation context)\n> "
        ).strip()
        if action_prompt:
            message = (
                "CONTEXT from a loaded resource:\n"
                "---\n"
                f"{resource_content}\n"
                "---\n"
                f"TASK: {action_prompt}"
            )
        else:
            print("No action specified. Adding resource content to conversation memory...")
            message = (
                "Please remember the following context for our conversation. "
                "Just acknowledge that you have received it.\n"
                "---\n"
                f"CONTEXT:\n{resource_content}\n"
                "---"
            )
        return message, "chat"

    return (user_input, "chat") if user_input else None
    
# Entry point
async def main():
    client = MultiServerMCPClient(server_configs)
    print("Starting MultiServerMCPClient...")
    all_tools = await client.get_tools()
    agent = create_graph(all_tools)
    print("MCP Agent is ready (connected to Weather and Task servers).")
    
    # Add instructions for the new prompt commands
    print("Type a question, or use one of the following commands:")
    print("  /prompts                                       - to list available prompts")
    print("  /prompt <server_name> <prompt_name> \"args\"   - to run a specific prompt") 
    print("  /resources                             - to list available resources")
    print("  /resource <server_name> <resource_uri>      - to load a resource for the agent")

    while True:
        user_input = input("\nYou: ").strip()
        if user_input.lower() in {"exit", "quit", "q"}:
            break

        request = await prepare_agent_request(client, user_input)
        if request is None:
            continue

        message, request_type = request
        config = prompt_config if request_type == "prompt" else chat_config
        try:
            response = await agent.ainvoke(
                {"messages": [("user", message)]},
                config=config
            )
            print("AI:", response["messages"][-1].content)
        except Exception as e:
            print("Error:", e)

if __name__ == "__main__":
    asyncio.run(main())
