import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, field_validator

mcp = FastMCP("github-server")

GITHUB_API = "https://api.github.com"
# No auth token required for these read-only public endpoints, but GitHub's
# unauthenticated rate limit is low (60 req/hour per IP).


# ============================================================
# STRUCTURED OUTPUT MODELS
# ============================================================

class RepoSummary(BaseModel):
    full_name: str = Field(description="Repository full name, e.g. 'octocat/Hello-World'")
    description: str | None = Field(description="Repository description")
    stars: int = Field(description="Star count")
    language: str | None = Field(description="Primary language")
    url: str = Field(description="HTML URL to the repository")


class SearchReposResult(BaseModel):
    success: bool = Field(description="Whether the search completed successfully")
    total_count: int = Field(default=0, description="Total matching repositories on GitHub")
    repos: list[RepoSummary] = Field(default_factory=list, description="Repos on this page")
    page: int = Field(default=1, description="Current page number (1-indexed)")
    per_page: int = Field(default=5, description="Results per page")
    has_more: bool = Field(default=False, description="Whether another page of results exists")
    error: str | None = Field(default=None, description="Explanation if the search failed")


class ReadmeResult(BaseModel):
    success: bool = Field(description="Whether the README was fetched successfully")
    repo: str = Field(description="Repository full name")
    content: str | None = Field(default=None, description="README content, possibly truncated")
    truncated: bool = Field(default=False, description="True if content was cut short of the full README")
    full_length: int | None = Field(default=None, description="Full README length in characters, even if truncated")
    error: str | None = Field(default=None, description="Explanation if the fetch failed")


# ============================================================
# INPUT MODELS WITH VALIDATION
# ============================================================

class SearchReposInput(BaseModel):
    query: str = Field(
        description="Search keywords, e.g. 'langgraph agents'. Cannot be empty or start with a special character.",
        min_length=1,
        max_length=200,
    )
    page: int = Field(default=1, description="Page number to fetch (1-indexed)", ge=1)
    per_page: int = Field(default=5, description="Results per page (1-10)", ge=1, le=10)

    @field_validator("query")
    @classmethod
    def query_must_be_valid(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("Query cannot be empty or whitespace-only")
        if not trimmed[0].isalnum():
            raise ValueError(f"Query must start with a letter or digit, not '{trimmed[0]}'")
        return trimmed


class GetReadmeInput(BaseModel):
    owner: str = Field(description="Repository owner/org, e.g. 'langchain-ai'", min_length=1)
    repo: str = Field(description="Repository name, e.g. 'langgraph'", min_length=1)
    max_length: int = Field(
        default=2000,
        description="Maximum characters of README content to return (1-20000). Use a small value to "
        "save tokens; call again with a larger max_length if you need more.",
        ge=1,
        le=20000,
    )


# ============================================================
# TOOLS
# ============================================================

@mcp.tool()
async def search_repos(input: SearchReposInput) -> SearchReposResult:
    """Search GitHub for public repositories matching a keyword query.

    Use this when the user wants to find repositories, libraries, or
    projects on GitHub related to a topic. Results are paginated — each
    call returns one page (per_page results, default 5) rather than
    everything GitHub matched. Check `has_more` in the response: if true,
    call again with page+1 to fetch the next page instead of requesting a
    huge per_page in one shot. Returns success=False with an explanation on
    failure (rate limit, network error).
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{GITHUB_API}/search/repositories",
                params={"q": input.query, "per_page": input.per_page, "page": input.page},
                headers={"Accept": "application/vnd.github+json"},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 403:
            error_msg = "GitHub API rate limit exceeded. Try again later."
        else:
            error_msg = f"GitHub API returned {exc.response.status_code} for query '{input.query}'."
        return SearchReposResult(success=False, page=input.page, per_page=input.per_page, error=error_msg)
    except httpx.RequestError as exc:
        return SearchReposResult(
            success=False, page=input.page, per_page=input.per_page,
            error=f"Network error reaching GitHub API: {exc}",
        )

    repos = [
        RepoSummary(
            full_name=item["full_name"],
            description=item.get("description"),
            stars=item["stargazers_count"],
            language=item.get("language"),
            url=item["html_url"],
        )
        for item in data.get("items", [])
    ]
    total_count = data.get("total_count", 0)
    # has_more: true if there are results beyond the current page
    has_more = (input.page * input.per_page) < total_count

    return SearchReposResult(
        success=True,
        total_count=total_count,
        repos=repos,
        page=input.page,
        per_page=input.per_page,
        has_more=has_more,
    )


@mcp.tool()
async def get_repo_readme(input: GetReadmeInput) -> ReadmeResult:
    """Fetch the README content of a specific GitHub repository, truncated
    to max_length characters by default to avoid returning huge payloads.

    Use this when the user wants to know what a specific repo does or how
    to use it. If `truncated` is true in the response, `full_length` tells
    you how much content exists in total — call again with a larger
    max_length if the truncated content isn't enough to answer the user's
    question, rather than assuming the README ended there.
    """
    repo_full_name = f"{input.owner}/{input.repo}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{GITHUB_API}/repos/{input.owner}/{input.repo}/readme",
                headers={"Accept": "application/vnd.github.raw+json"},
            )
            resp.raise_for_status()
            full_content = resp.text
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            error_msg = f"Repository '{repo_full_name}' not found, or it has no README."
        else:
            error_msg = f"GitHub API returned {exc.response.status_code} for '{repo_full_name}'."
        return ReadmeResult(success=False, repo=repo_full_name, error=error_msg)
    except httpx.RequestError as exc:
        return ReadmeResult(success=False, repo=repo_full_name, error=f"Network error reaching GitHub API: {exc}")

    full_length = len(full_content)
    is_truncated = full_length > input.max_length
    content = full_content[: input.max_length] if is_truncated else full_content

    return ReadmeResult(
        success=True,
        repo=repo_full_name,
        content=content,
        truncated=is_truncated,
        full_length=full_length,
    )


# ============================================================
# RESOURCES
# ============================================================

@mcp.resource("github://rate-limit")
async def get_rate_limit() -> str:
    """Expose current GitHub API rate-limit status as read-only context."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{GITHUB_API}/rate_limit")
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        return f"Could not fetch rate limit: {exc}"

    core = data["resources"]["core"]
    return f"Remaining: {core['remaining']}/{core['limit']} (resets at {core['reset']})"


if __name__ == "__main__":
    mcp.run(transport="streamable-http")