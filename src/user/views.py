#
# user/views.py
#
#
import flask
import http

from flask_login import login_required
from flask_login import current_user

import controller
from main import app
import schema



@app.get("/public-profile")
@login_required
def get_self_public_profie():
    """
    Fetch a public user page
    """
    return flask.render_template("public-profile.html", user=current_user.user)


@app.get("/user/<slug>")
def get_user_public_profile(slug):
    """
    Fetch a public user page
    """
    # ensure we're lowercase
    slug = slug.lower()

    user = schema.user.User.get_by_slug(slug)
    if user is None:
        flask.abort(http.HTTPStatus.NOT_FOUND)

    return flask.render_template("public-profile.html", user=user)


@app.get("/edit-account")
@login_required
def get_edit_account():
    user = current_user.user
    form = controller.get_edit_form(user.type, user)
    if form is None:
        flask.abort(http.HTTPStatus.BAD_REQUEST)
    return flask.render_template("forms/edit-account.html",
            user=user, form=form, stripe_key=app.config['STRIPE_PUB_KEY'])


@app.post("/edit-account")
@login_required
def post_edit_account():
    """
    Update *your* account
    """
    user = current_user.user
    form = controller.get_edit_form(user.type)
    if form.validate():
        controller.update(user, form)

    return flask.render_template("forms/edit-account.html",
            user=user, form=form, stripe_key=app.config['STRIPE_PUB_KEY'])
