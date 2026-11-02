"""Deterministic, fact-preserving RFQ drafts."""
from config import settings

def header(value):
    return str(value or '').replace('\r', '').replace('\n', '')

def footer():
    # Blank identity fields are omitted, never rendered as empty lines.
    lines = [settings.RFQ_BUYER_NAME, settings.RFQ_BUYER_COMPANY, settings.RFQ_BUYER_ADDRESS, 'reply unsubscribe to stop']
    return '\n'.join(line for line in lines if line)

def draft(campaign, supplier, reference, followup=False, chinese=None):
    if chinese is None:
        chinese = (supplier.get('country') or '').strip().lower() in {'china', 'cn', 'taiwan', 'tw', 'hong kong', 'hk', '中国', '台湾', '香港'}
    product = campaign['product']
    details = '\n'.join((f'{label}: {campaign[key]}' for key, label in [('specification', 'Specification'), ('quantity', 'Quantity'), ('unit', 'Unit'), ('target_incoterm', 'Requested incoterm'), ('destination', 'Destination'), ('quote_deadline', 'Quote deadline')] if campaign.get(key)))
    certs = campaign.get('required_certifications') or []
    if certs:
        details += '\nRequired certifications: ' + ', '.join(certs)
    opening = f'Following up on our earlier request, could you send your quotation for {product}?' if followup else f'We are sourcing {product} and would like your quotation.'
    english = f"Hello,\n\n{opening}\n{details}\nPlease state unit price, currency, MOQ, lead time, incoterm, payment terms, validity, sample and tooling costs."
    body = english
    if chinese:
        name_zh = campaign.get('product_zh') or product
        opening_zh = f'您好，之前向贵司询问过{name_zh}，请问方便提供报价吗？' if followup else f'您好，我们正在采购{name_zh}，请贵司提供报价。'
        body = f"{opening_zh}\n请注明单价、币种、最小起订量、交货时间、贸易条款、付款条件及报价有效期。\n\n" + english
    body += f'\n\nReference: {reference}\n{footer()}'
    return {'language': 'zh-en' if chinese else 'en', 'subject': header(f"[{reference}] {('Follow-up: ' if followup else '')}Request for quotation: {product}"), 'body': body}
