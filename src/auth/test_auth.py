#
# auth/test_auth.py
#
#
import flask
import json

import controller
import main
import schema
import utils


def test_register_and_login(client, mail):
    email = 'testme22@hello.com'
    register = {
        'slug' : 'testme22',
        'password' : 'password',
        'confirm' : 'password',
        'email' : email,
        'type' : 'seller',
    }

    r = client.post('/api/register', data=register)
    assert utils.isok(r.status_code)

    login = {
        'name' : 'testme22',
        'password' : 'password',
    }

    r = client.post('/api/login', data=login)
    assert utils.isok(r.status_code)
    assert r.headers.get('set-cookie')
    d = json.loads(r.data)
    assert d['user']['email'] == email

    user = schema.user.User.get_by_email(email)
    assert user is not None

    token = controller.generate_token(email, user.salt)
    r = client.get('/auth/email/confirm/%s' % token)
    assert utils.isredirect(r.status_code)

    user = schema.user.User.get_by_email(email)
    assert user is not None

    assert user.email_confirmed


def test_token(mail):
    token = controller.generate_token("me@me.com", "mysalt")
    email = controller.confirm_token(token, "mysalt")
    assert email == "me@me.com"

    controller.send_confirm_email("testme", "me@me.com", token)

    # FIXME: figure out how to verify email in outbox


