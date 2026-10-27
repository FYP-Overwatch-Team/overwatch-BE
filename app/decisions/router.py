from datetime import date

from fastapi import Depends, Query

from app.api.deps import workspace_member
from app.core.constants import InboxTab
from app.core.time import utcnow
from app.core.responses import WithMeta, api_router
from app.decisions import checks, overview, service, suggest
from app.decisions.schemas import (
    ChangeCheckSettings,
    ChangeStatus,
    CheckResult,
    CheckSettings,
    CreateDecision,
    Decision,
    InboxCounts,
    NewEvidence,
    OverviewPage,
    SuggestRequest,
    UpdateDecision,
)

# any member records and settles decisions; roles come later
router = api_router(prefix="/decisions", tags=["decisions"])


@router.get("")
async def list_decisions(
    tab: InboxTab | None = None,
    today: date | None = None,
    q: str | None = Query(default=None, max_length=200),
    ref_id: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    member: dict = Depends(workspace_member()),
) -> WithMeta[list[Decision]]:
    """`tab` is an inbox tab for this member, `today` their date (UTC when not sent) for review dates;
    `ref_id` narrows to the decisions quoting that board item or page."""
    decisions, total = await service.list_decisions(
        member, tab=tab, today=today or utcnow().date(), q=q, ref_id=ref_id, offset=offset, limit=limit,
    )
    return WithMeta(decisions, {"total": total, "offset": offset, "limit": limit})


@router.get("/counts")
async def inbox_counts(today: date | None = None, member: dict = Depends(workspace_member())) -> InboxCounts:
    """How many decisions each inbox tab holds for this member."""
    return InboxCounts(**await service.counts(member, today or utcnow().date()))


@router.post("", status_code=201)
async def create_decision(body: CreateDecision, member: dict = Depends(workspace_member())) -> Decision:
    return await service.create(member, body)


@router.post("/suggest", status_code=201)
async def suggest_decisions(body: SuggestRequest, member: dict = Depends(workspace_member())) -> list[Decision]:
    """Ask the AI to draft the decisions a board item or page states; they're saved as proposals to review."""
    return await suggest.suggest(member, body.kind, body.ref_id)


@router.get("/checks")
async def get_check_settings(member: dict = Depends(workspace_member())) -> CheckSettings:
    return await checks.get_settings(member["workspace_id"])


@router.patch("/checks")
async def change_check_settings(body: ChangeCheckSettings, member: dict = Depends(workspace_member())) -> CheckSettings:
    return await checks.change_settings(member["workspace_id"], body)


@router.post("/checks/run")
async def run_check(member: dict = Depends(workspace_member())) -> CheckResult:
    """Check accepted decisions against what changed, now, instead of waiting for the next run."""
    return await checks.check_workspace(member["workspace_id"], member["user_id"])


@router.post("/overview")
async def update_overview(member: dict = Depends(workspace_member())) -> OverviewPage:
    """Write the product overview page from the accepted decisions, creating it the first time."""
    return OverviewPage(page_id=await overview.update(member))


@router.get("/{decision_id}")
async def get_decision(decision_id: str, member: dict = Depends(workspace_member())) -> Decision:
    return await service.get(member["workspace_id"], decision_id)


@router.patch("/{decision_id}")
async def update_decision(decision_id: str, body: UpdateDecision, member: dict = Depends(workspace_member())) -> Decision:
    return await service.update(member, decision_id, body)


@router.post("/{decision_id}/status")
async def change_status(decision_id: str, body: ChangeStatus, member: dict = Depends(workspace_member())) -> Decision:
    return await service.change_status(member, decision_id, body)


@router.post("/{decision_id}/evidence", status_code=201)
async def add_evidence(decision_id: str, body: NewEvidence, member: dict = Depends(workspace_member())) -> Decision:
    return await service.add_evidence(member, decision_id, body)


@router.delete("/{decision_id}/evidence/{evidence_id}")
async def remove_evidence(decision_id: str, evidence_id: str, member: dict = Depends(workspace_member())) -> Decision:
    return await service.remove_evidence(member, decision_id, evidence_id)


@router.post("/{decision_id}/dismiss")
async def dismiss_suggestion(decision_id: str, member: dict = Depends(workspace_member())) -> None:
    """"Not a decision": removes an AI suggestion still waiting for review, so it isn't suggested again."""
    await suggest.dismiss(member, decision_id)
