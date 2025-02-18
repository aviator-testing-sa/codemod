#!/usr/bin/python

import config
import datetime
import jinja2
import logging
import os
import functools
import flask
from flask import Flask
from flask import abort
from flask import redirect
from flask import render_template
from flask import request
from flask import session
from flask_login import LoginManager
from flask_login import current_user
from flask_sqlalchemy import SQLAlchemy
from raven.contrib.flask import Sentry
from user.current_user import CurrentUser
from user.current_user import Anonymous

'''
The main application setup. The order of things is important
in this file.
'''
from flask_wtf.csrf import CsrfProtect
csrf = CsrfProtect()

def create_app(testing=False):
    app = Flask(__name__, static_folder='../static', template_folder='../templates')
    app.config.from_object('config.base')
    app.config.from_envvar('APP_CONFIG_FILE')
    app.config['APP_ROOT'] = os.path.dirname(os.path.abspath(__file__))
    app.config['ENVIRONMENT'] = os.environ.get('ENVIRONMENT', 'dev')

    # monkey patch some easy-to-use routes
    app.get = functools.partial(app.route, methods=['GET'])
    app.post = functools.partial(app.route, methods=['POST'])
    app.delete = functools.partial(app.route, methods=['DELETE'])
    app.put = functools.partial(app.route, methods=['PUT'])

    return app

app = create_app()


'''
Initialize database
'''
def create_db(app):
    return SQLAlchemy(app)

db = create_db(app)


'''
Initialize models. This import makes sure all the models
are defined and parsed by SQL Alchemy.
'''
import schema


'''
Initialize mail

def create_mail(app):
    import flask.ext.mail
    mail = flask.ext.mail.Mail()
    mail.init_app(app)
    return mail

mail = create_mail(app)
'''

'''
Initialize sentry and enable logging.
'''
sentry = Sentry(app, logging=True, level=logging.ERROR,
    dsn=app.config.get('SENTRY_DSN', ''))

if app.config['DEBUG']:
    logging.getLogger().addHandler(logging.StreamHandler())

'''
Initialize the login manager
'''
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.anonymous_user = Anonymous

@login_manager.user_loader
def load_user(userid):
    user = schema.user.User.query.get(userid)
    if user:
        return CurrentUser(user)
    return None

@login_manager.unauthorized_handler
def unauthorized():
    return redirect('/login')


'''
Initialize all views so that all the route are set correctly.
'''
from main import urls


'''
Initialize flask request variables
'''
from main import base

@app.errorhandler(404)
def page_not_found(e):
    return "Could not find page", 404


@app.template_filter('escape_custom')
def escape_custom(string):
    if not string:
        return ''
    string = string.encode('ascii', 'ignore')
    string = str(jinja2.escape(string))
    return jinja2.Markup(string.replace('\n', '<br/>\n'))

@app.template_filter('number')
def number(string):
    if string == 0:
        return '0'
    elif not string:
        return ''
    val = int(string)
    return "{:,}".format(val)

@app.template_filter('cents_number')
def cents_number(string):
    if string == 0:
        return '0'
    elif not string:
        return jinja2.Markup('&mdash;')
    val = int(string)
    return "$ " + "{:,}".format(val / 100)
