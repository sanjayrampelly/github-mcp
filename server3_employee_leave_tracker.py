import datetime

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import String, Integer, Date, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# ============================================================
# DATABASE LAYER (SQLAlchemy async — same pattern as your day job)
# ============================================================

DATABASE_URL = "sqlite+aiosqlite:///leave_tracker.db"
engine = create_async_engine(DATABASE_URL, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    leave_balance: Mapped[int] = mapped_column(Integer, default=20)  # days/year


class LeaveRequest(Base):
    __tablename__ = "leave_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(Integer)
    start_date: Mapped[datetime.date] = mapped_column(Date)
    end_date: Mapped[datetime.date] = mapped_column(Date)
    days: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/approved/rejected
    reason: Mapped[str] = mapped_column(String(300), default="")


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # seed one employee if the table's empty, so tools work immediately
    async with SessionLocal() as session:
        result = await session.execute(select(Employee).where(Employee.id == 1))
        if result.scalar_one_or_none() is None:
            session.add(Employee(id=1, name="Sanjay", leave_balance=20))
            await session.commit()


# ============================================================
# MCP SERVER
# ============================================================

mcp = MCPServer("leave-tracker-server")


# ---- STRUCTURED MODELS ----

class LeaveRequestOut(BaseModel):
    id: int
    employee_id: int
    start_date: str
    end_date: str
    days: int
    status: str
    reason: str


class ApplyLeaveInput(BaseModel):
    employee_id: int = Field(description="ID of the employee applying for leave", ge=1)
    start_date: str = Field(description="Leave start date, format YYYY-MM-DD")
    end_date: str = Field(description="Leave end date, format YYYY-MM-DD")
    reason: str = Field(default="", description="Optional reason for the leave", max_length=300)

    @field_validator("start_date", "end_date")
    @classmethod
    def valid_date_format(cls, v: str) -> str:
        try:
            datetime.date.fromisoformat(v)
        except ValueError:
            raise ValueError(f"'{v}' is not a valid date in YYYY-MM-DD format")
        return v


class ApplyLeaveResult(BaseModel):
    success: bool
    request: LeaveRequestOut | None = None
    error: str | None = None


class ApproveLeaveInput(BaseModel):
    request_id: int = Field(description="ID of the leave request to approve or reject", ge=1)
    approve: bool = Field(description="True to approve, False to reject")


class ApproveLeaveResult(BaseModel):
    success: bool
    request: LeaveRequestOut | None = None
    error: str | None = None


class LeaveBalanceResult(BaseModel):
    success: bool
    employee_id: int
    name: str | None = None
    leave_balance: int | None = None
    error: str | None = None


# ---- TOOLS ----

@mcp.tool()
async def apply_leave(input: ApplyLeaveInput) -> ApplyLeaveResult:
    """Submit a new leave request for an employee.

    Use this when the user wants to apply for, request, or book time off.
    Validates the employee exists and has enough remaining leave balance
    before creating the request in 'pending' status. Does not deduct the
    balance yet — that happens only when the request is approved.
    """
    start = datetime.date.fromisoformat(input.start_date)
    end = datetime.date.fromisoformat(input.end_date)

    if end < start:
        return ApplyLeaveResult(success=False, error="end_date cannot be before start_date")

    days = (end - start).days + 1

    try:
        async with SessionLocal() as session:
            employee = await session.get(Employee, input.employee_id)
            if employee is None:
                return ApplyLeaveResult(
                    success=False, error=f"No employee found with id {input.employee_id}"
                )
            if days > employee.leave_balance:
                return ApplyLeaveResult(
                    success=False,
                    error=(
                        f"Requested {days} day(s) exceeds remaining balance "
                        f"of {employee.leave_balance} day(s)"
                    ),
                )

            leave = LeaveRequest(
                employee_id=input.employee_id,
                start_date=start,
                end_date=end,
                days=days,
                status="pending",
                reason=input.reason,
            )
            session.add(leave)
            await session.commit()
            await session.refresh(leave)

            return ApplyLeaveResult(
                success=True,
                request=LeaveRequestOut(
                    id=leave.id,
                    employee_id=leave.employee_id,
                    start_date=str(leave.start_date),
                    end_date=str(leave.end_date),
                    days=leave.days,
                    status=leave.status,
                    reason=leave.reason,
                ),
            )
    except Exception as exc:
        return ApplyLeaveResult(success=False, error=f"Database error: {exc}")


@mcp.tool()
async def approve_leave(input: ApproveLeaveInput) -> ApproveLeaveResult:
    """Approve or reject a pending leave request.

    Use this when a manager/approver wants to act on a leave request. On
    approval, deducts the requested days from the employee's leave balance.
    Rejected requests do not affect the balance. Fails cleanly if the
    request doesn't exist or was already actioned.
    """
    try:
        async with SessionLocal() as session:
            leave = await session.get(LeaveRequest, input.request_id)
            if leave is None:
                return ApproveLeaveResult(
                    success=False, error=f"No leave request found with id {input.request_id}"
                )
            if leave.status != "pending":
                return ApproveLeaveResult(
                    success=False,
                    error=f"Request {input.request_id} is already '{leave.status}', not pending",
                )

            if input.approve:
                employee = await session.get(Employee, leave.employee_id)
                if employee is None:
                    return ApproveLeaveResult(
                        success=False,
                        error=f"Employee {leave.employee_id} for this request no longer exists",
                    )
                if leave.days > employee.leave_balance:
                    return ApproveLeaveResult(
                        success=False,
                        error="Employee's balance is now insufficient to approve this request",
                    )
                employee.leave_balance -= leave.days
                leave.status = "approved"
            else:
                leave.status = "rejected"

            await session.commit()
            await session.refresh(leave)

            return ApproveLeaveResult(
                success=True,
                request=LeaveRequestOut(
                    id=leave.id,
                    employee_id=leave.employee_id,
                    start_date=str(leave.start_date),
                    end_date=str(leave.end_date),
                    days=leave.days,
                    status=leave.status,
                    reason=leave.reason,
                ),
            )
    except Exception as exc:
        return ApproveLeaveResult(success=False, error=f"Database error: {exc}")


@mcp.tool()
async def get_leave_balance(employee_id: int = Field(description="Employee ID to check", ge=1)) -> LeaveBalanceResult:
    """Check an employee's remaining leave balance.

    Use this when the user wants to know how many leave days someone has
    left. Only counts approved leave against the balance — pending requests
    do not reduce it yet.
    """
    try:
        async with SessionLocal() as session:
            employee = await session.get(Employee, employee_id)
            if employee is None:
                return LeaveBalanceResult(
                    success=False, employee_id=employee_id, error=f"No employee found with id {employee_id}"
                )
            return LeaveBalanceResult(
                success=True,
                employee_id=employee_id,
                name=employee.name,
                leave_balance=employee.leave_balance,
            )
    except Exception as exc:
        return LeaveBalanceResult(success=False, employee_id=employee_id, error=f"Database error: {exc}")


# ---- RESOURCES ----

@mcp.resource("leave://pending")
async def get_pending_requests() -> str:
    """Expose all pending leave requests as read-only context, for an approver's queue."""
    try:
        async with SessionLocal() as session:
            result = await session.execute(
                select(LeaveRequest).where(LeaveRequest.status == "pending")
            )
            pending = result.scalars().all()
    except Exception as exc:
        return f"Could not fetch pending requests: {exc}"

    if not pending:
        return "No pending leave requests"
    return "\n".join(
        f"#{r.id} — employee {r.employee_id}: {r.start_date} to {r.end_date} "
        f"({r.days} day(s)) — {r.reason or 'no reason given'}"
        for r in pending
    )


# ---- PROMPTS ----

@mcp.prompt()
def review_pending_leaves() -> str:
    """Prompt template for reviewing the pending leave queue."""
    return (
        "Review the pending leave requests. For each, note whether it looks "
        "reasonable given typical team capacity, and flag any that overlap "
        "with other pending requests from the same period."
    )


if __name__ == "__main__":
    import asyncio

    asyncio.run(init_db())  # create tables + seed employee before serving
    mcp.run(transport="streamable-http")