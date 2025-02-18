import flask
import flask_wtf
import hashlib
import http.client as httplib
import itsdangerous
import logging
import os
import re
import requests
import schema
import time

import main
from main import app
from main import db
from user.current_user import CurrentUser
import utils

from sqlalchemy import select
from sqlalchemy.orm import Session


def login_user(user, remember=False):
    flask.ext.login.login_user(CurrentUser(user), remember=remember)

def logout_user():
    flask.ext.login.logout_user()

def get_user_by_name(name):
    """
    Fetch by email or username
    """
    # user = schema.user.User.get_by_email(name)
    with Session(db.engine) as session:
        user = session.execute(select(schema.user.User).where(schema.user.User.email == name)).scalar_one_or_none()
    if user is None:
        # user = schema.user.User.get_by_slug(name)
        with Session(db.engine) as session:
            user = session.execute(select(schema.user.User).where(schema.user.User.slug == name)).scalar_one_or_none()
    return user

def login(name, password, remember):
    """
    Login with either slug or email
    """
    user = get_user_by_name(name)
    if user is None:
        return None
    if user.password != utils.encrypt_with_salt(password, user.salt):
        return None

    # session-enabled login
    login_user(user, remember)

    return user


def register(form):
    """
    Register via wtforms
    """
    salt, password = utils.encrypt_with_new_salt(form.password.data)

    user = schema.user.User(
        slug=form.slug.data,
        email=form.email.data,
        password=password,
        salt=salt,
        type=form.type.data,
        status=schema.user.UserStatus.new)

    with Session(db.engine) as session:
        session.add(user)
        session.commit()

    # go ahead and login
    login_user(user)

    # send the confirm email
    send_confirm_email_by_user(user)

    return user


def reset_password(user, password_raw):
    """
    """
    salt, password = utils.encrypt_with_new_salt(password_raw)

    user.password = password
    user.salt = salt

    with Session(db.engine) as session:
        session.add(user)
        session.commit()

    login_user(user)

    return user


def generate_token(field, salt):
    """
    Generate a timed confirmation token; use salt stored in user table

    >>> token = generate_token('me@me.com', 'mysalt')
    >>> token is not None
    True
    """
    serializer = itsdangerous.URLSafeTimedSerializer(app.config['AUTH_SECRET_KEY'])
    return serializer.dumps(field, salt=salt)


def confirm_token(token, salt, expiration=3600):
    """
    Validate confirmation token; use salt stored in user table

    >>> token = generate_token('me@me.com', 'mysalt')
    >>> confirm_token(token, 'mysalt')
    'me@me.com'
    """
    serializer = itsdangerous.URLSafeTimedSerializer(app.config['AUTH_SECRET_KEY'])
    try:
        field = serializer.loads(token, salt=salt, max_age=expiration)
    except:
        return None
    return field


def create_confirm_email(username, email, token):
    url = flask.url_for('confirm_email', token=token, _external=True)
    html = flask.render_template('emails/email_confirm.html',
            username=username, confirm_url=url, support_email=app.config['SUPPORT_EMAIL'])

    '''
    msg = flask.ext.mail.Message(
        sender=app.config['SUPPORT_EMAIL'],
        recipients=[email],
        subject="Activate account on Reinvent",
        html=html)
    return msg
    '''

def send_confirm_email(username, email, token):
    #msg = create_confirm_email(username, email, token)
    #return mail.send(msg)
    pass

def send_confirm_email_by_user(user):
    token = generate_token(user.email, user.salt)
    return send_confirm_email(user.display_name, user.email, token)

#
# password reset related
#
def create_reset_email(username, email, token):
    url = flask.url_for('confirm_reset_password', token=token, email=email, _external=True)

    html = flask.render_template('emails/forgot_password.html',
        url=url,
        username=username,
        support_email=app.config['SUPPORT_EMAIL'])

    '''
    msg = flask.ext.mail.Message(
        sender=app.config['SUPPORT_EMAIL'],
        recipients=[email],
        subject="Reset your password on Reinvent",
        html=html)

    return msg
    '''


def send_reset_email_by_user(user):
    #token = generate_token(user.email, app.config['AUTH_RESET_SALT'])
    #msg = create_reset_email(user.display_name, user.email, token)
    #return mail.send(msg)
    pass


def validate_email(email):
    # Check if the user is already registered.
    # user = schema.User.query.filter_by(email_id=email).first()
    with Session(db.engine) as session:
        user = session.execute(select(schema.User).filter_by(email_id=email)).scalar_one_or_none()
    if user:
        raise UserWarning("Email Already exists")
    if not _is_valid_email(email):
        raise UserWarning("Invalid email address")


def send_password_reset(user, reset_password_url):
    emails.forgot_password(user, reset_password_url)


def _is_valid_email(email):
    email = email.lower()
    _email_re = re.compile(r'[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}')
    match = _email_re.match(email)
    return bool(match)


#
#
#
def build_auth_linkedin(confirm_url=None, csrf=None):
    if confirm_url is None:
        confirm_url = flask.url_for('linkedin_confirm', _external=True)
    if csrf is None:
        csrf = flask_wtf.csrf.generate_csrf()

    url = utils.buildurl('https://www.linkedin.com/oauth/v2/authorization',
        response_type='code',
        client_id=app.config['LINKEDIN_CLIENT_ID'],
        redirect_uri=confirm_url,
        state=csrf)

    return url


def get_access_linkedin(code, confirm_url=None):
    if confirm_url is None:
        confirm_url = flask.url_for('linkedin_confirm', _external=True)

    url = utils.buildurl('https://www.linkedin.com/oauth/v2/accessToken')
    data = {
        'grant_type' : 'authorization_code',
        'code' : code,
        'redirect_uri' : confirm_url,
        'client_id' : app.config['LINKEDIN_CLIENT_ID'],
        'client_secret' : app.config['LINKEDIN_CLIENT_SECRET'],
    }

    r = requests.post(url, data=data)
    if not r.ok:
        logging.error(r.content)
        flask.abort(httplib.FORBIDDEN)

    return r.json()


def get_profile_linkedin(token, name='~', fields=['email-address', 'picture-url', 'last-name', 'first-name']):
    if fields:
        profile = '%s:(%s)' % (name, ','.join(fields))
    else:
        profile = name

    url = utils.buildurl('https://api.linkedin.com/v1/people', profile, format='json')
    headers = { 'Authorization' : 'Bearer %s' % token}
    r = requests.get(url, headers=headers)
    if not r.ok:
        logging.error(r.content)
        flask.abort(httplib.BAD_REQUEST)

    return r.json()
