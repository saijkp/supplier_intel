"""Bounded Meta payload parser. Extra provider fields are ignored, never stored."""
import re
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra='ignore')


class Metadata(Strict):
    phone_number_id: str


class Message(Strict):
    id: str = Field(min_length=1, max_length=256, pattern=r'^[A-Za-z0-9_.=:+/-]+$')
    from_: str = Field(alias='from', pattern=r'^[1-9][0-9]{6,14}$')
    timestamp: str = Field(pattern=r'^[0-9]{1,12}$')
    type: str = Field(min_length=1,max_length=40,pattern=r'^[a-z_]+$')
    text: dict | None = None


class Value(Strict):
    messaging_product: str
    metadata: Metadata
    messages: list[Message] = Field(default_factory=list,max_length=100)
    statuses: list[dict] = Field(default_factory=list,max_length=100)


class Change(Strict):
    field: str
    value: Value


class Entry(Strict):
    id: str
    changes: list[Change] = Field(min_length=1,max_length=20)


class Envelope(Strict):
    object: str
    entry: list[Entry] = Field(min_length=1,max_length=20)


def parse_payload(raw, config):
    envelope = Envelope.model_validate_json(raw)
    if envelope.object != 'whatsapp_business_account':
        raise ValueError('Invalid object')
    result = []
    for entry in envelope.entry:
        if entry.id != config['waba']:
            raise ValueError('Account not allowed')
        for change in entry.changes:
            value = change.value
            if change.field != 'messages' or value.messaging_product != 'whatsapp' or value.metadata.phone_number_id != config['phone']:
                raise ValueError('Event not allowed')
            for message in value.messages:
                body = None
                if message.type == 'text':
                    body = (message.text or {}).get('body')
                    if not isinstance(body,str) or not 1 <= len(body) <= 4096:
                        raise ValueError('Invalid text')
                result.append(dict(provider_id=message.id,sender=message.from_,kind=message.type,body=body,sent_at=message.timestamp))
                if len(result)>100:
                    raise ValueError('Too many messages')
    return result
