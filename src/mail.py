"""mail service: inbound catcher (pop/reset/clear), outbound send (SES), and
the attachment store. API Gateway proxy handler: `mail.handler`.

The S3-triggered receiver is a separate function: `mail_receive.handler`.
"""
from common import run
import mail_attachments
import mail_inbox
import mail_send

ROUTES = {
    'pop': mail_inbox.pop,
    'reset': mail_inbox.reset,
    'clear': mail_inbox.clear,
    'send': mail_send.send,
    'attachments/save': mail_attachments.save,
    'attachments/get': mail_attachments.get,
    'attachments/delete': mail_attachments.delete,
    'attachments/find': mail_attachments.find,
    'attachments/text': mail_attachments.text,
    'attachments/compare': mail_attachments.compare,
    'attachments/upload': mail_attachments.upload_page,
    'attachments/upload/url': mail_attachments.upload_url,
}

INDEX = {
    'description': 'Inbound mail catcher (any address at the mail domain), outbound '
                   'send via SES, and an attachment store with text search.',
}


def handler(event, context):
    return run(event, 'mail', ROUTES, index=INDEX)
