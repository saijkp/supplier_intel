"""Authenticated RFQ endpoints using the existing shared-secret access model."""
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from api.auth import require_api_token
from sourcing.workspace_review import reviewed
from storage.quote_workspace_repository import QuoteWorkspaceRepository

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class RFQInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: Text
    customer: str = Field(default='', max_length=500)
    quantity: Decimal = Field(gt=0, allow_inf_nan=False)
    unit: Text = 'pcs'
    mandatory_specs: dict[Text, Text] = Field(default_factory=dict, max_length=50)


class QuoteInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    supplier_name: Text
    supplier_id: int | None = Field(default=None, gt=0)
    source_text: str = Field(default='', max_length=50000)
    fields: dict[Text, str | int | float | None] = Field(default_factory=dict, max_length=40)
    quoted_specs: dict[Text, Text] = Field(default_factory=dict, max_length=50)
    evidence: dict[Text, str] = Field(default_factory=dict, max_length=100)
    deviations: list[Text] = Field(default_factory=list, max_length=50)

    @field_validator('fields', mode='before')
    @classmethod
    def reject_boolean_values(cls, values):
        if isinstance(values, dict) and any(isinstance(v, bool) for v in values.values()):
            raise ValueError('Quote fields cannot be boolean values')
        return values


def get_rfq_repo():
    return QuoteWorkspaceRepository()


router = APIRouter(prefix='/rfqs', tags=['RFQ workspace'], dependencies=[Depends(require_api_token)])


def load(rid, repo):
    rfq = repo.get_rfq(rid)
    if rfq is None:
        raise HTTPException(404, 'RFQ not found')
    return rfq


@router.get('')
def list_rfqs(repo=Depends(get_rfq_repo)):
    return repo.list_rfqs()


@router.post('', status_code=201)
def create_rfq(body: RFQInput, repo=Depends(get_rfq_repo)):
    return reviewed(repo.create_rfq(body.model_dump(mode='json')))


@router.get('/{rid}')
def get_rfq(rid: str, repo=Depends(get_rfq_repo)):
    return reviewed(load(rid, repo))


@router.post('/{rid}/quotes', status_code=201)
def add_quote(rid: str, body: QuoteInput, repo=Depends(get_rfq_repo)):
    load(rid, repo)
    if body.supplier_id is not None:
        from rfq.workspace_workflow import WorkspaceWorkflow
        workflow=WorkspaceWorkflow(repo.db_path)
        view=workflow.view(rid)
        if not view['linked'] or not any(m['supplier_id']==body.supplier_id for m in view['campaign']['messages']):
            raise HTTPException(422,'Supplier must belong to the linked outreach campaign')
    # Evidence must be an actual excerpt of the preserved supplier response.
    for key, excerpt in body.evidence.items():
        if not excerpt.strip() or excerpt not in body.source_text:
            raise HTTPException(422, f'Evidence for {key} must appear in the original supplier response')
    repo.add_quote(rid, body.model_dump(mode='json'))
    return reviewed(load(rid, repo))
