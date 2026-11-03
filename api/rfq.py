"""Authenticated RFQ endpoints using the existing shared-secret access model."""
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from api.auth import require_api_token
from sourcing.quote_validation import MasterRFQ, SupplierQuote, validate_quote
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


ZH = {
    'unit_price': '单价', 'currency': '报价币种', 'quoted_quantity': '本次报价对应的数量',
    'moq': '最小起订量（MOQ）', 'incoterm': '贸易条款（如 EXW、FOB）',
    'named_place': '贸易条款对应的交货地点或港口', 'production_lead_days': '生产交期（天）',
    'payment_terms': '付款条件', 'valid_until': '报价有效期（YYYY-MM-DD）',
}


def reviewed(rfq):
    master = MasterRFQ(rfq['id'], 1, Decimal(rfq['quantity']), rfq['mandatory_specs'])
    for q in rfq['quotes']:
        quote = SupplierQuote(q['supplier_name'], rfq['id'], 1, q['fields'], q['quoted_specs'], q['evidence'], q['deviations'])
        review = validate_quote(master, quote).to_dict()
        questions = []
        for key in review['missing_fields']:
            evidence = key.endswith('_evidence')
            field = key[:-9] if evidence else key
            label = '规格 ' + field[5:] if field.startswith('spec:') else ZH.get(field, field)
            questions.append(('请提供原始报价依据以确认：' if evidence else '请确认：') + label + '。')
        for key, expected in rfq['mandatory_specs'].items():
            actual = q['quoted_specs'].get(key)
            if actual and actual.strip().casefold() != expected.strip().casefold():
                questions.append(f'规格「{key}」要求为「{expected}」，贵司报价为「{actual}」。请确认能否按要求提供，并说明价格及交期变化。')
        quoted_quantity = q['fields'].get('quoted_quantity')
        if quoted_quantity is not None and any(d.startswith('quantity:') for d in review['deviations']):
            questions.append(f'本次询价数量为 {rfq["quantity"]} {rfq["unit"]}，贵司报价数量为 {quoted_quantity}。请按询价数量重新确认报价。')
        for deviation in q['deviations']:
            questions.append('请确认是否可按询价要求提供，并说明此项差异：' + deviation)
        error_zh = {
            'Unsupported Incoterm': '请提供有效的国际贸易条款。',
            'Quoted quantity falls below supplier MOQ': '报价数量低于最小起订量，请确认是否可接受本次订单数量。',
            'currency must be a three-letter uppercase currency code': '请以三位大写代码确认报价币种（如 USD、CNY）。',
            'Quotation validity date has expired': '报价已过有效期，请更新报价并确认新的有效日期。',
            'valid_until must be a valid YYYY-MM-DD date': '请按 YYYY-MM-DD 格式确认有效的报价截止日期。',
            'production_lead_days must be non-negative': '请以不小于零的天数确认生产交期。',
        }
        for error in review['errors']:
            if error.endswith(' must be a positive number'):
                questions.append('请确认有效的' + ZH.get(error.split(' ')[0], error.split(' ')[0]) + '（必须大于零）。')
            else:
                questions.append(error_zh.get(error, '请核对并更正以下报价问题：' + error))
        q['review'] = review
        q['clarification_zh'] = ('您好，关于「' + rfq['title'] + '」的报价，请协助确认以下事项：\n' +
            '\n'.join(f'{i + 1}. {s}' for i, s in enumerate(questions)) + '\n谢谢！') if questions else ''
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
    # Evidence must be an actual excerpt of the preserved supplier response.
    for key, excerpt in body.evidence.items():
        if not excerpt.strip() or excerpt not in body.source_text:
            raise HTTPException(422, f'Evidence for {key} must appear in the original supplier response')
    repo.add_quote(rid, body.model_dump(mode='json'))
    return reviewed(load(rid, repo))
