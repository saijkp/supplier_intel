"""Candidate-field validation and bounded, offline document reading."""
import io
import pytest
from openpyxl import Workbook
from pypdf import PdfWriter
from rfq.quote_parser import QuoteParser,strip_history,numeric_present
from rfq.attachments import extract_text,MAX_FILE_BYTES,MAX_TEXT

class LLM:
    def __init__(self,result): self.result=result
    def complete_json(self,*args): return self.result

def parse(source,**fields):
    return QuoteParser(LLM({'quotes':[fields]})).parse(source)

@pytest.mark.parametrize('history',['> USD 100','On Monday Alice wrote:\nUSD 100','-----Original Message-----\nUSD 100','发件人: buyer\nUSD 100','发件人：buyer\nUSD 100'])
def test_strip_quoted_history(history):
    assert strip_history('New reply\n'+history).strip()=='New reply'

@pytest.mark.parametrize('value,proof',[(1234,'EUR 1,234'),(1.25,'EUR 1,25'),(1234.5,'EUR 1,234.50'),(1234.5,'EUR 1.234,50'),(0,'Free tooling 0')])
def test_numeric_locale_evidence(value,proof):
    assert numeric_present(value,proof)

@pytest.mark.parametrize('value,proof',[(3,'USD 30'),(12,'USD 2'),(-1,'USD 1'),(float('nan'),'nan'),(True,'1'),('invalid','USD 2')])
def test_numeric_invalid(value,proof):
    assert not numeric_present(value,proof)

@pytest.mark.parametrize('currency,normalized',[('RMB','CNY'),('CNY','CNY'),('元','CNY'),('US$','USD'),('USD','USD'),('$','$'),('¥','¥'),('EUR','EUR')])
def test_currency_stated_only_and_allowed_normalization(currency,normalized):
    source=f'{currency} 5 each'
    result=parse(source,unit_price=5,currency=currency,evidence={'unit_price':source,'currency':source})
    assert result['quotes'][0]['currency']==normalized


def test_missing_evidence_and_unstated_values_dropped():
    result=parse('Price 5; lead time 4 weeks',unit_price=5,currency='USD',moq=10,lead_time='28 days',incoterm='FOB',tooling_cost=0,evidence={'unit_price':'Price 5','currency':'Price 5','moq':'10','lead_time':'4 weeks','incoterm':'FOB','tooling_cost':'Price 5'})
    assert result['quotes']==[{'unit_price':5.0,'evidence':{'unit_price':'Price 5'}}]


def test_whitespace_normalization_and_lead_time_kept_as_stated():
    source='USD   2.50\nLead time: 4\n weeks'
    result=parse(source,unit_price=2.5,lead_time='4 weeks',evidence={'unit_price':'USD 2.50','lead_time':'4 weeks'})
    assert result['quotes'][0]['lead_time']=='4 weeks'

@pytest.mark.parametrize('fields',[{'currency':'USD','evidence':{'currency':'USD'}},{'unit_price':5,'evidence':{'unit_price':'invented 5'}},{'unit_price':9,'evidence':{'unit_price':'USD 5'}},{'unit_price':5,'evidence':{}}])
def test_no_valid_price_no_quote(fields):
    assert parse('USD 5',**fields)=={'quotes':[],'parse_status':'no_quote'}


def test_multitier_and_malformed_llm():
    assert QuoteParser(LLM({'quotes':[None,{'unit_price':1,'evidence':{'unit_price':'USD 1'}},{'unit_price':2,'evidence':{'unit_price':'USD 2'}}]})).parse('USD 1 and USD 2')['parse_status']=='parsed'
    for value in (None,[],{}, {'quotes':'bad'}): assert QuoteParser(LLM(value)).parse('data')['parse_status']=='failed'


def test_quoted_own_quantity_not_source_evidence():
    result=parse('Thanks\n> requested 1000 units',unit_price=1000,evidence={'unit_price':'1000 units'})
    assert result['parse_status']=='no_quote'


def test_all_nonprice_fields_grounded():
    values={'quantity_basis':'per 100 pieces','moq':100,'lead_time':'4–6 weeks','incoterm':'FOB Ningbo','payment_terms':'30% deposit','valid_until':'31 December','sample_terms':'samples free','tooling_cost':200}
    source='USD 2; '+'; '.join(str(v) for v in values.values())
    result=parse(source,unit_price=2,**values,evidence={'unit_price':'USD 2',**{k:str(v) for k,v in values.items()}})
    for field,value in values.items(): assert result['quotes'][0][field]==value

@pytest.mark.parametrize('name,data',[('quote.txt',b'USD 5'),('quote.csv',b'price,currency\n5,USD')])
def test_text_and_csv(name,data):
    text,status=extract_text(name,data)
    assert '5' in text and status=='read'


def test_xlsx_read_only_and_text_bound():
    book=Workbook();book.active.append(['USD',5]);buffer=io.BytesIO();book.save(buffer);book.close()
    text,status=extract_text('quote.xlsx',buffer.getvalue())
    assert 'USD\t5' in text and status=='read'
    assert len(extract_text('quote.txt',b'x'*(MAX_TEXT+100))[0])==MAX_TEXT


def test_scanned_empty_pdf_and_unreadable_files():
    writer=PdfWriter();writer.add_blank_page(width=100,height=100);buffer=io.BytesIO();writer.write(buffer)
    for name,payload in [('scan.pdf',buffer.getvalue()),('bad.pdf',b'garbage'),('quote.docx',b'unsupported'),('quote.txt',b'\xff')]:
        assert extract_text(name,payload)[1]=='needs_manual_review'
    assert extract_text('big.txt',b'x'*(MAX_FILE_BYTES+1))[1]=='needs_manual_review'

@pytest.mark.parametrize('value,evidence',[(1234,'USD 1.234'),(1234,'USD 12,34'),(5,'USD -5')])
def test_numeric_evidence_does_not_change_decimal_places_or_sign(value,evidence):
    assert not numeric_present(value,evidence)


def test_currency_must_be_a_stated_token():
    result=parse('USDT 5',unit_price=5,currency='USD',evidence={'unit_price':'USDT 5','currency':'USDT 5'})
    assert 'currency' not in result['quotes'][0]
