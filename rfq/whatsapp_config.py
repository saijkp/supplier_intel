"""Inbound WhatsApp configuration. No access token or outbound capability."""
from config import settings


def configuration():
    return {
        'enabled': settings.WHATSAPP_INBOUND_ENABLED,
        'secret': settings.WHATSAPP_APP_SECRET,
        'verify': settings.WHATSAPP_VERIFY_TOKEN,
        'waba': '1713595540331361',
        'phone': '1383583781512437',
    }
