from fastapi import Depends, Query

from app.api.deps import workspace_member
from app.core.constants import DecisionStatus
from app.core.responses import WithMeta, api_router
from app.decisions import checks, service, suggest
from app.decisions.schemas import (
    ChangeCheckSettings,
    ChangeStatus,
    CheckResult,
    CheckSettings,
    CreateDecision,
    Decision,
    NewEvidence,
    SuggestRequest,
    UpdateDecision,
)

# any member records and settles decisions; roles come later
router = api_router(prefix="/decisions", tags=["decisions"])


@router.get("")
async def list_decisions(
    status: DecisionStatus | None = None,
    q: str | None = Query(default=None, max_length=200),
    ref_id: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    member: dict = Depends(workspace_member()),
) -> WithMeta[list[Decision]]:
    """`ref_id` narrows to the decisions quoting that board item or page."""
    decisions, total = await service.list_decisions(
        member["workspace_id"], status=status, q=q, ref_id=ref_id, offset=offset, limit=limit,
    )
    return WithMeta(decisions, {"total": total, "offset": offset, "limit": limit})


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


@router.put("/checks")
async def change_check_settings(body: ChangeCheckSettings, member: dict = Depends(workspace_member())) -> CheckSettings:
    return await checks.set_enabled(member["workspace_id"], body.enabled)


@router.post("/checks/run")
async def run_check(member: dict = Depends(workspace_member())) -> CheckResult:
    """Check accepted decisions against what changed, now, instead of waiting for the next run."""
    return await checks.check_workspace(member["workspace_id"], member["user_id"])


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
