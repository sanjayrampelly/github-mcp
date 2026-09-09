import asyncio
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import ToolNode
from langgraph.graph import StateGraph, MessagesState, END
from langchain_groq import ChatGroq
from dotenv import load_dotenv
load_dotenv()  # Load environment variables from .env file

async def build_graph():
    # Step 1 & 2: connect + discover tools.
    # get_tools() sends tools/list under the hood and converts each MCP
    # tool schema into a LangChain Tool object automatically.
    mcp_client = MultiServerMCPClient({
        "leave_tracker": {
            "url": "http://127.0.0.1:8000/mcp",
            "transport": "streamable_http",
        }
    })
    tools = await mcp_client.get_tools()
    print("Discovered tools:", [t.name for t in tools])

    # Step 3: bind the discovered tools to Claude.
    # bind_tools() is what actually sends {name, description, input_schema}
    # to the model on every call — this is identical whether the tools came
    # from MCP or a local @tool decorator, as we discussed earlier.
    llm = ChatGroq(model="openai/gpt-oss-safeguard-20b")
    llm_with_tools = llm.bind_tools(tools)

    def call_model(state: MessagesState):
        # This is where step 3 (send query + tools) and step 6 (send tool
        # results back) both happen — same node, called repeatedly by the loop.
        response = llm_with_tools.invoke(state["messages"])
        return {"messages": [response]}

    def should_continue(state: MessagesState):
        # This is the non-deterministic decision point: did Claude choose
        # to call a tool, or is it done?
        last = state["messages"][-1]
        return "tools" if last.tool_calls else END

    graph = StateGraph(MessagesState)
    graph.add_node("agent", call_model)
    # Step 4 & 5: ToolNode executes the tool. Because these tools came from
    # MultiServerMCPClient, executing them sends a tools/call request to
    # Server 3 over HTTP — not a local Python function call.
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge("tools", "agent")  # loop back so Claude sees the result
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", should_continue)
    return graph.compile()


async def main():
    app = await build_graph()

    result = await app.ainvoke({
        "messages": [{"role": "user", "content": "How many leave days does employee 1 have left?"}]
    })

    for m in result["messages"]:
        print(f"--- {m.type} ---")
        print(m.content)


if __name__ == "__main__":
    asyncio.run(main())