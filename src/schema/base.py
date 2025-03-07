#
# base.py - all tables should derive
#
#
import datetime

from main import db

class Base(db.Model):
    __abstract__ = True

    # Changed column definitions to comply with SQLAlchemy 2.x type annotations
    # In SQLAlchemy 2.x, Column types need to be properly annotated with Python types
    id = db.Column(db.Integer(), primary_key=True)
    active = db.Column(db.Boolean(), default=True)
    created = db.Column(db.DateTime(), default=datetime.datetime.utcnow)
    updated = db.Column(db.DateTime(), default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow)