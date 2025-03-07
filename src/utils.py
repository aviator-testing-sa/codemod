#
# utils.py - various things
#
#
import binascii
import datetime
import functools
import hashlib
# Changed httplib (Python 2) to http.client (Python 3) for compatibility with SQLAlchemy 2.1
import http.client
import json
import os
import re
# Updated to Python 3 style import for urllib to be compatible with SQLAlchemy 2.1
import urllib.parse
from unicodedata import normalize

#
# some decorators
#
def response(encoder, *opts, **kwopts):
    def _fn(fn):
        @functools.wraps(fn)
        def __fn(*args, **kwargs):
            data = fn(*args, **kwargs)
            return encoder(data, *opts, **kwopts)
        return __fn
    return _fn

def _json_encoder_response(data, **kwargs):
    import flask
    r = flask.jsonify(data)
    # Changed iteritems() to items() for Python 3 compatibility required with SQLAlchemy 2.1
    for k,v in kwargs.items():
        setattr(r, k, v)
    return r

response_json = functools.partial(response, _json_encoder_response)


def form_errors(form):
    """ Returns a single string from all errors """
    error_message = ''
    for field, errors in form.errors.items():
        for error in errors:
            error_message += "%s - %s.<br/>" % (getattr(form, field).name, error)
    return error_message

#
# helpers
#
def isok(code):
    """
    Status code is in the 200 range
    """
    return code >= 200 and code < 300

def isredirect(code):
    return code >= 300 and code < 400

def abort(code, **kwargs):
    """
    Abort wrapper to return more descriptive errors
    """
    import flask
    return flask.abort(code, response=flask.jsonify(**kwargs))

def jsonify(code=http.client.OK, **kwargs):
    import flask
    r = flask.jsonify(**kwargs)
    r.status_code = code
    return r

def slugify(text, delim=u'-'):
    """Generates an slightly worse ASCII-only slug."""
    _punct_re = re.compile(r'[\t !"#$%&\'()*\-/<=>?@\[\\\]^_`{|},.]+')
    result = []
    for word in _punct_re.split(text.lower()):
        word = normalize('NFKD', word).encode('ascii', 'ignore')
        if word:
            result.append(word)
    # Changed unicode() to str() for Python 3 compatibility required with SQLAlchemy 2.1
    return str(delim.join(result))


def buildurl(base, *args, **kwargs):
    if base[-1] != '/':
        base = base + '/'
    if args:
        jargs = os.path.join(*(str(a) for a in args))
        endpoint = base + jargs
    else:
        endpoint = base

    def _gen():
        # Changed iteritems() to items() for Python 3 compatibility required with SQLAlchemy 2.1
        for k,v in kwargs.items():
            if not isinstance(v, (tuple, list)):
                v = v,
            for val in v:
                # Changed urllib.quote_plus to urllib.parse.quote_plus for Python 3 compatibility
                yield "%s=%s" % (k, urllib.parse.quote_plus(str(val)))

    return "?".join([ endpoint, "&".join(_gen()) ])





#
# cryptographic
#
def encrypt_with_salt(string, salt):
    return hashlib.sha1(string + salt).hexdigest()

def encrypt_with_new_salt(string, bytes=16):
    salt = binascii.b2a_hex(os.urandom(bytes))
    return salt, encrypt_with_salt(string, salt)


#
# print date on form
#
def pretty_date(time):
    """
    Get a datetime object or a int() Epoch timestamp and return a
    pretty string like 'an hour ago', 'Yesterday', '3 months ago',
    'just now', etc
    """
    now = datetime.datetime.now()
    if type(time) is int:
        diff = now - datetime.datetime.fromtimestamp(time)
    elif isinstance(time, datetime.datetime):
        diff = now - time
    elif not time:
        diff = now - now
    second_diff = diff.seconds
    day_diff = diff.days

    if day_diff < 0:
        return ''

    if day_diff == 0:
        if second_diff < 10:
            return "just now"
        if second_diff < 60:
            return str(second_diff) + " seconds ago"
        if second_diff < 120:
            return "a minute ago"
        if second_diff < 3600:
            return str(second_diff / 60) + " minutes ago"
        if second_diff < 7200:
            return "an hour ago"
        if second_diff < 86400:
            return str(second_diff / 3600) + " hours ago"
    if day_diff == 1:
        return "Yesterday"
    if day_diff < 7:
        return str(day_diff) + " days ago"
    if day_diff < 31:
        return str(day_diff / 7) + " weeks ago"
    if day_diff < 365:
        return str(day_diff / 30) + " months ago"
    return str(day_diff / 365) + " years ago"
