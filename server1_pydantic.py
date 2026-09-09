from mcp.server.fastmcp import FastMCP, Context
from pydantic import BaseModel, Field, field_validator

mcp = FastMCP("tasks-server")

tasks: list[dict] = []


# ---- STRUCTURED OUTPUT MODELS ----
# Defining explicit output schemas (instead of returning raw strings) is what
# lets a client-side team consume your tool predictably — they get typed,
# documented fields instead of parsing free text.

class Task(BaseModel):
    id: int = Field(description="Unique task identifier")
    title: str = Field(description="The task's title")
    done: bool = Field(description="Whether the task has been completed")


class AddTaskResult(BaseModel):
    task: Task = Field(description="The task that was created")
    total_tasks: int = Field(description="Total number of tasks after this addition")


class CompleteTaskResult(BaseModel):
    success: bool = Field(description="Whether the task was found and marked complete")
    task: Task | None = Field(default=None, description="The updated task, if found")
    message: str = Field(description="Human-readable result message")

class AddTaskInput(BaseModel):
    title: str = Field(
        description="The task title. Must be non-empty, under 200 characters, "
        "and start with a letter or digit (no leading special characters).",
        min_length=1,
        max_length=200,
    )

 
    @field_validator("title")
    @classmethod
    def title_must_be_valid(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("Title cannot be empty or whitespace-only")
        if not trimmed[0].isalnum():
            raise ValueError(
                f"Title must start with a letter or digit, not '{trimmed[0]}'"
            )
        return trimmed

# ---- TOOLS ----

@mcp.tool()
async def add_task(
    input: AddTaskInput,
    ctx: Context = None,
) -> AddTaskResult:
    """Add a new task to the task list.

    Use this when the user wants to create, log, or track a new to-do item.
    The task starts as not-done. Returns the created task along with the
    updated total task count, so the caller can confirm the addition without
    a separate list_tasks call.
    """
    task = {"id": len(tasks) + 1, "title": input.title, "done": False}
    tasks.append(task)
    if ctx is not None:
        await ctx.session.send_resource_list_changed()
    return AddTaskResult(task=Task(**task), total_tasks=len(tasks))


@mcp.tool()
def complete_task(
    task_id: int = Field(description="The numeric ID of the task to mark complete.", ge=1),
) -> CompleteTaskResult:
    """Mark an existing task as complete, given its numeric ID.

    Use this when the user says they've finished, done, or completed a
    specific task. If no task with the given ID exists, returns
    success=False with an explanatory message rather than raising an error,
    so the calling LLM can relay a clean failure to the user.
    """
    for t in tasks:
        if t["id"] == task_id:
            t["done"] = True
            return CompleteTaskResult(
                success=True,
                task=Task(**t),
                message=f"Task {task_id} marked complete.",
            )
    return CompleteTaskResult(
        success=False,
        task=None,
        message=f"No task found with id {task_id}.",
    )


# ---- RESOURCES (read-only context, not actions) ----

@mcp.resource("tasks://all")
def get_all_tasks() -> str:
    """Expose the current task list as read-only context."""
    if not tasks:
        return "No tasks yet"
    return "\n".join(
        f"[{'x' if t['done'] else ' '}] {t['id']}: {t['title']}" for t in tasks
    )


# ---- PROMPTS (reusable templates) ----

@mcp.prompt()
def summarize_tasks() -> str:
    """Prompt template for summarizing the current task list."""
    return (
        "Summarize the current task list, grouping by done/pending, "
        "and suggest a priority order."
    )


if __name__ == "__main__":
    mcp.run(transport="streamable-http")

# to start the server, run: python server1_pydantic.py for streamble-http transport
# on the other terminal, run: npx @modelcontextprotocol/inspector and then connect the streamable-http transport to the inspector. You can then call the tools and see the structured output in the inspector.

# for stdio transport, run: MCP dev server1_pydantic.py and then in another terminal, run: MCP dev inspector 