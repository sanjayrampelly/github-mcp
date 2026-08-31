from mcp.server.mcpserver import MCPServer
import asyncio

mcp = MCPServer("tasks-server")

tasks: list[dict] = []

# ---- TOOLS (callable actions) ----
@mcp.tool()
async def add_task(title: str) -> str:
    """Add a new task"""
    task = {"id": len(tasks) + 1, "title": title, "done": False}
    tasks.append(task)
    # NOTIFICATION: tell client the resource list changed
    # await mcp.get_context().session.send_resource_list_changed()
    return f"Added task {task['id']}: {title}"

@mcp.tool()
async def complete_task(task_id: int) -> str:
    """Mark a task complete"""
    for t in tasks:
        if t["id"] == task_id:
            t["done"] = True
            # NOTIFICATION: tell client the resource list changed
            # await mcp.get_context().session.send_resource_list_changed()
            return f"Task {task_id} marked complete"
    return f"Task {task_id} not found"

# ---- RESOURCES (read-only context, not actions) ----
@mcp.resource("tasks://all")
def get_all_tasks() -> str:
    """Expose current task list as context"""
    return "\n".join(f"[{'x' if t['done'] else ' '}] {t['id']}: {t['title']}" for t in tasks) or "No tasks yet"

# ---- PROMPTS (reusable templates) ----
@mcp.prompt()
def summarize_tasks() -> str:
    """Prompt template for summarizing tasks"""
    return "Summarize the current task list, grouping by done/pending, and suggest priority order."

if __name__ == "__main__":
    mcp.run(transport="stdio")