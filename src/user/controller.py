#
# user/controller.py
#
#
import logging

from main import db
import forms
import forms.user
import schema
from upload import uploader



def get_edit_form(type, user=None):
    if type == schema.user.UserType.seller:
        return forms.user.EditSeller(obj=user)

    if type == schema.user.UserType.buyer:
        return forms.user.EditBuyer(obj=user)

    if type == schema.user.UserType.admin:
        raise NotImplementedError

    return None


def update(user, form):
    data = dict(form.data)
    data.pop('image')

    for k,v in data.iteritems():
        setattr(user, k, v)

    if form.image.data:
        # FIXME: if we have a user image already, then we need to remove it
        if user.image:
            pass

        user.image = uploader.s3_upload(form.image.data)

    db.session.commit()

