"""Select only an explicitly named supplier's messages from a chat export."""
import re

START=re.compile(r'^\[?(\d{1,4}[/.\-]\d{1,2}[/.\-]\d{1,4}),?\s+(\d{1,2}:\d{2}(?::\d{2})?(?:\s*[APap][Mm])?)\]?\s*(?:-\s*)?([^:]+):\s?(.*)$')
TIMESTAMP=re.compile(r'^\[?\d{1,4}[/.\-]\d{1,2}[/.\-]\d{1,4}')


def supplier_messages(text,supplier_sender):
    sender=supplier_sender.strip().casefold()
    if not sender or ':' in sender: raise ValueError('Enter the exact supplier sender name from the chat export')
    selected=[];current=None
    for raw in text.splitlines():
        line=re.sub('[\u200e\u200f\u202a-\u202e]','',raw)
        match=START.match(line)
        if match:
            current=None
            if match.group(3).strip().casefold()==sender:
                current={'timestamp':match.group(1)+' '+match.group(2),'sender':match.group(3).strip(),'text':match.group(4)}
                selected.append(current)
        elif TIMESTAMP.match(line):
            current=None  # System messages must not become supplier evidence.
        elif current is not None:
            current['text']+='\n'+line
    selected=[m for m in selected if m['text'].strip()]
    if not selected: raise ValueError('No messages matched that supplier name. Check the export format and exact sender name.')
    if len(selected)>100: raise ValueError('Import at most 100 supplier messages at a time; select the relevant RFQ conversation first')
    return selected
