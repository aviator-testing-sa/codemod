import httplib
import itertools
import logging
import emails

from auth import controller
import flask
import flask_wtf
from flask import jsonify
from flask import redirect
from flask import render_template
from flask import request
from flask import session
from flask import url_for
from flask.ext.login import current_user
from flask.ext.login import login_user
from flask.ext.login import logout_user
from flask.ext.login import login_required

from forms.reset_password import ResetPasswordForm
from main import app, db
from user.current_user import CurrentUser

import forms.auth
import schema
import utils

import requests


@app.post('/api/login')
def api_login():
    form = forms.auth.Login(request.form, csrf_enabled=False)
    if not form.validate():
        utils.abort(httplib.BAD_REQUEST, errors=form.errors)

    user = controller.login(form.name.data, form.password.data, form.remember.data)
    if user is None:
        utils.abort(httplib.NOT_FOUND)

    return utils.jsonify(user=user.encode(mode=schema.user.EncodeMode.mine))


@app.post('/api/register')
def api_register():
    form = forms.auth.Signup(request.form, csrf_enabled=False)
    if not form.validate():
        utils.abort(httplib.BAD_REQUEST, errors=form.errors)

    # register by form
    user = controller.register(form)

    return utils.jsonify(httplib.CREATED, user=user.encode(mode=schema.user.EncodeMode.mine))



@app.route('/login', methods=['GET', 'POST'])
def login():
    return redirect('/signup')
'''
    form = forms.auth.Login()
    signup_form = forms.auth.Signup()
    if not form.validate_on_submit():
        return render_template('forms/login.html', form=form, signup_form=signup_form)

    next = flask.request.args.get('next')

    # FIXME: next_is_valid should check if the user has valid
    # permission to access the `next` url
#    if not next_is_valid(next):
#        return flask.abort(400)

    user = controller.login(form.name.data, form.password.data, form.remember.data)
    if not user:
        return render_template('forms/login.html', form=form, signup_form=signup_form,
                error='Invalid username/email or password')

    return redirect('/')
'''

'''
@app.route('/register', methods=['GET', 'POST'])
def register():
    return render_template('forms/signup.html')


    form = forms.auth.Signup()
    if not form.validate_on_submit():
        return render_template('forms/login.html', signup_form=form)

    # register by form
    user = controller.register(form)

    return redirect('/')
'''

@app.get('/signup')
def signup():
    return render_template('forms/signup.html', auth_linked=controller.build_auth_linkedin())


@app.get('/listing/create/signup')
def listing_signup():
    if current_user.is_authenticated:
        return redirect('/listing/create/product')

    session['next_url'] = '/listing/create/product'
    return render_template('forms/listing_signup.html', auth_linked=controller.build_auth_linkedin())


@app.get('/logout')
def logout():
    controller.logout_user()
    return redirect('/')


@app.get('/auth/email/send')
@login_required
def send_confirm_email():
    controller.send_confirm_email_by_user(current_user.user)
    return jsonify(success=True)


@app.get('/auth/email/confirm/<token>')
@login_required
def confirm_email(token):
    email = controller.confirm_token(token, current_user.user.salt)
    if email is None:
        flask.flash('The confirmation link is invalid or has expired.', 'danger')
        flask.abort(httplib.FORBIDDEN)

    # some validation
    user = schema.user.User.get_by_email(email)
    assert user is not None
    assert user.email == current_user.user.email

    # confirm if not already confirmed
    if not user.email_confirmed:
        user.email_confirmed = True
        db.session.add(user)
        db.session.commit()

    return redirect('/')


@app.post('/auth/reset/send')
def send_reset_password():
    # discover user by parameter
    email = flask.request.form.get('email')
    user = schema.user.User.get_by_email(email)
    if user is not None:
        controller.send_reset_email_by_user(user)
    return redirect(url_for('login'))


@app.route('/auth/reset/confirm', methods=['GET', 'POST'])
def confirm_reset_password():
    email = flask.request.args.get('email')
    token = flask.request.args.get('token')
    confirm_email = controller.confirm_token(token, app.config['AUTH_RESET_SALT'])
    if confirm_email is None:
        flask.flash('The confirmation link is invalid or has expired.', 'danger')
        flask.abort(httplib.FORBIDDEN)

    # hmm... security issue
    if confirm_email != email:
        logging.error("Someone tried reseting with mismatched email: %s != %s", confirm_email, email)
        flask.flash('The confirmation link is invalid or has expired.', 'danger')
        flask.abort(httplib.FORBIDDEN)

    # lookup user
    user = schema.user.User.get_by_email(email)
    if user is None:
        flask.flash('Invalid email')
        flask.abort(httplib.BAD_REQUEST)

    # generate the url
    url = flask.url_for('confirm_reset_password', token=token, email=email)

    form = forms.auth.ResetPasswordForgot()
    if not form.validate_on_submit():
        return render_template('forms/forgot_password.html', form=form, user=user)

    # success
    controller.reset_password(user, form.password.data)
    flask.flash("Password reset")

    return flask.redirect('/')


#
#
#
@app.get("/auth/linkedin/confirm")
def linkedin_confirm():
    csrf = flask.request.args.get('state')
    if not flask_wtf.csrf.validate_csrf(csrf):
        app.abort(httplib.FORBIDDEN)

    # linkedin athentication code
    code = flask.request.args.get('code')

    # request access token
    data = controller.get_access_linkedin(code)
    token = data['access_token']
    expires = data['expires_in']

    # fetch profile
    profile = controller.get_profile_linkedin(token)
    email = profile['emailAddress']
    names = []
    if profile['firstName']:
        names.append(profile['firstName'])
    if profile['lastName']:
        names.append(profile['lastName'])
    fullname = " ".join(names)
    image = profile.get('pictureUrl')

    # if we already exist, then we're doing an update
    user = schema.user.User.get_by_email(email)
    new_signup = False
    if user is None:
        # slugify
        slug = schema.user.User.validate_slug(utils.slugify(unicode(fullname)))
        user = schema.user.User(
            slug=slug,
            email=email,
            status=schema.user.UserStatus.new)
        new_signup = True

    user.fullname = fullname
    if image:
        user.image = image
    user.email_confirmed = True     # infer the email is confirmed

    # store linkedin auth code
    # FIXME: stash access token & expiration in current user OR db?
    user.auth_linkedin = code

    db.session.add(user)
    db.session.commit()

    # login
    controller.login_user(user)

    # send email
    if new_signup:
        emails.welcome(user)
    return redirect(session.get('next_url') or '/')




def login_as(user_id):
    user = base_controller.get_user(user_id)
    _login_user(user)
    return redirect('/')


def validate_email():
    email = request.args.get('email', '')
    try:
        controller.validate_email(email)
    except UserWarning as e:
        return jsonify(success=False, error=e.message)
    return jsonify(success=True)



def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    email = request.form.get('email', '').strip()
    success = False
    error = False
    if email:
        user = base_controller.get_user_by_email(email)
        if user:
            token = controller.get_password_reset_sig(user.email_id)
            reset_password_url = url_for('reset_password',
                sig=token, email=user.email_id, _external=True)
            controller.send_password_reset(user, reset_password_url)
            success='Password reset instructions sent on email.'
        else:
            error='Cannot find email.'

    if request.is_xhr:
        return jsonify(success=bool(success), error=error)

    return render_template('auth/forgot_password.html', success=success, error=error)


def reset_password():
    email = request.args.get('email', '')
    sig = request.args.get('sig', '')
    if not email or sig != controller.get_password_reset_sig(email):
        return abort(403)

    user = base_controller.get_user_by_email(email)
    _login_user(user)

    params = {}
    form = ResetPasswordForm(request.form)
    if request.method == 'POST' and form.validate(forgot=True):
        controller.reset_password(user, form.password.data)
        form = ResetPasswordForm()
        success_message = 'Your password has been changed successfully !'
        params['success_message'] = success_message

    params['form'] = form
    params['forgot_password'] = True
    return render_template('forms/reset_password.html', **params)


@app.route('/change_password')
@login_required
def change_password():
    form = ResetPasswordForm(request.form)

    success_message = ''
    if request.method == 'POST' and form.validate():
        controller.reset_password(current_user.user, form.password.data)

        # Reset form
        form = ResetPasswordForm()
        success_message = 'Your password has been changed successfully !'
    return render_template('forms/reset_password.html', form=form, success_message=success_message)


def _login_user(user, remember=True):
    logged_in_user = CurrentUser(user)
    login_user(logged_in_user, remember=remember)

