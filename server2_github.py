import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, field_validator

mcp = FastMCP("github-server")

GITHUB_API = "https://api.github.com"

# No auth token required for these read-only public endpoints, but GitHub's
# unauthenticated rate limit is low (60 req/hour per IP). If you hit 403s,
# that's the rate limit, not a bug — we can add token auth later.


# ---- STRUCTURED OUTPUT MODELS ----

class RepoSummary(BaseModel):
    full_name: str = Field(description="Repository full name, e.g. 'octocat/Hello-World'")
    description: str | None = Field(description="Repository description")
    stars: int = Field(description="Star count")
    language: str | None = Field(description="Primary language")
    url: str = Field(description="HTML URL to the repository")


class SearchReposResult(BaseModel):
    success: bool = Field(description="Whether the search completed successfully")
    total_count: int = Field(default=0, description="Total matching repositories on GitHub")
    repos: list[RepoSummary] = Field(default_factory=list, description="Top results returned")
    error: str | None = Field(default=None, description="Explanation if the search failed")


class ReadmeResult(BaseModel):
    success: bool = Field(description="Whether the README was fetched successfully")
    repo: str = Field(description="Repository full name")
    content: str | None = Field(default=None, description="Decoded README content, if found")
    error: str | None = Field(default=None, description="Explanation if the fetch failed")


# ---- INPUT MODELS WITH VALIDATION ----

class SearchReposInput(BaseModel):
    query: str = Field(
        description="Search keywords, e.g. 'langgraph agents'. "
        "Cannot be empty or start with a special character.",
        min_length=1,
        max_length=200,
    )
    max_results: int = Field(
        default=5,
        description="Number of results to return (1-10).",
        ge=1,
        le=10,
    )

    @field_validator("query")
    @classmethod
    def query_must_be_valid(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("Query cannot be empty or whitespace-only")
        if not trimmed[0].isalnum():
            raise ValueError(
                f"Query must start with a letter or digit, not '{trimmed[0]}'"
            )
        return trimmed


class GetReadmeInput(BaseModel):
    owner: str = Field(description="Repository owner/org, e.g. 'langchain-ai'", min_length=1)
    repo: str = Field(description="Repository name, e.g. 'langgraph'", min_length=1)


# ---- TOOLS ----

@mcp.tool()
async def search_repos(input: SearchReposInput) -> SearchReposResult:
    """Search GitHub for public repositories matching a keyword query.

    Use this when the user wants to find repositories, libraries, or
    projects on GitHub related to a topic (e.g. "find repos about RAG
    pipelines"). Returns star count and language for each result so the
    caller can judge relevance/popularity without a follow-up call. If the
    search fails (e.g. rate limit, network issue), returns success=False
    with an explanation rather than raising an error.
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{GITHUB_API}/search/repositories",
                params={"q": input.query, "per_page": input.max_results},
                headers={"Accept": "application/vnd.github+json"},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 403:
            error_msg = "GitHub API rate limit exceeded. Try again later."
        else:
            error_msg = f"GitHub API returned {exc.response.status_code} for query '{input.query}'."
        return SearchReposResult(success=False, error=error_msg)
    except httpx.RequestError as exc:
        return SearchReposResult(
            success=False,
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
    return SearchReposResult(success=True, total_count=data.get("total_count", 0), repos=repos)


@mcp.tool()
async def get_repo_readme(input: GetReadmeInput) -> ReadmeResult:
    """Fetch and decode the README content of a specific GitHub repository.

    Use this when the user wants to know what a specific repo does, how to
    use it, or wants a summary of its documentation. Requires the exact
    owner and repo name (use search_repos first if the user only gives a
    vague description). If the repo doesn't exist or has no README, returns
    success=False with an explanation rather than raising an error.
    """
    repo_full_name = f"{input.owner}/{input.repo}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{GITHUB_API}/repos/{input.owner}/{input.repo}/readme",
                headers={"Accept": "application/vnd.github.raw+json"},
            )
            resp.raise_for_status()
            content = resp.text
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            error_msg = f"Repository '{repo_full_name}' not found, or it has no README."
        else:
            error_msg = f"GitHub API returned {exc.response.status_code} for '{repo_full_name}'."
        return ReadmeResult(success=False, repo=repo_full_name, error=error_msg)
    except httpx.RequestError as exc:
        return ReadmeResult(
            success=False,
            repo=repo_full_name,
            error=f"Network error reaching GitHub API: {exc}",
        )

    return ReadmeResult(success=True, repo=repo_full_name, content=content)


# ---- RESOURCES ----

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
    # streamable-http, matching the real deployment shape (server reachable
    # over a URL) rather than stdio's "client spawns you as a subprocess".
    mcp.run(transport="streamable-http")