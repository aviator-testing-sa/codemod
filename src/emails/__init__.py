import logging
import sendgrid
from . import templates
from main import app
from main import sentry
from flask import render_template
from sendgrid.helpers.mail import Content
from sendgrid.helpers.mail import Email
from sendgrid.helpers.mail import Mail
from sendgrid.helpers.mail import Personalization


def welcome(user):
    send_email('welcome',
        params={'user': user},
        to_email=user.email,
        to_name=user.fullname)

def listing_complete(user, listing):
    send_email('listing_complete',
        params={
            'user': user,
            'listing': listing,
        },
        to_email=user.email,
        to_name=user.fullname)


def send_email(template_name,
        params={},
        to_email='',
        to_name='',
        to=[], # Used for sending to multiple users
        cc=[],
        bcc=[],
        from_email=app.config['BASE_EMAIL'],
        from_name=app.config['BASE_FROM_NAME']):

    template = templates.MAP.get(template_name)
    if not template:
        logging.error('Invalid template name: ' + template_name)
        return

    if to_email and '@' not in to_email:
        logging.error('No email-address:' + to_email)
        return

    if not to_email and not to:
        logging.error('No sender')
        return

    mail = Mail()
    p = Personalization()
    if not to:
        to = [{'email': to_email, 'name': to_name, 'type': 'to'}]

    for user in to:
        p.add_to(Email(user['email'], user['name']))
    for user in cc:
        p.add_cc(Email(user['email'], user['name']))
    for user in bcc:
        p.add_bcc(Email(user['email'], user['name']))
    p.add_bcc(Email(app.config['BASE_BCC_EMAIL']))
    html_email = render_template(template.get('html'), **params)

    mail.set_from(Email(from_email, from_name))
    mail.set_subject(template.get('subject') % params)
    mail.add_content(Content("text/html", html_email))
    mail.add_personalization(p)
    sg = _get_client()
    try:
        response = sg.client.mail.send.post(request_body=mail.get())
    except Exception as e:
        sentry.captureException()


def _get_client():
    return sendgrid.SendGridAPIClient(apikey=app.config['SENDGRID_API_KEY'])