#
#
#
#
import contextlib
import os
import pytest
import main
import sqlalchemy
from sqlalchemy import event
from sqlalchemy.orm import Session

#TESTDB = 'test_project.db'
#TESTDB_PATH = "/tmp/{}".format(TESTDB)
#TEST_DATABASE_URI = 'postgresql+psycopg2://localhost/reinvent.testing'

@pytest.fixture(scope="session", autouse=True)
def app():
    app = main.app
    app.config['TESTING'] = app.testing = True
    app.secret_key = app.config['APP_SECRET']
    return app


@pytest.fixture(scope='session')
def db(app, request):
    """Session-wide test database."""
    db = main.db
    return db


@pytest.yield_fixture(scope='session')
def mailserver(app):
    import mailserver
    server = mailserver.debug_server(app.config['MAIL_SERVER'], app.config['MAIL_PORT'])
    with contextlib.closing(server):
        yield server


@pytest.fixture(scope='function')
def mail(app, mailserver, request):
    mail = main.create_mail(app)
    main.mail = mail
    return mail


@pytest.fixture(scope='function')
def session(db):
    """Creates a new database session for a test."""
    connection = db.engine.connect()
    transaction = connection.begin()

    session = Session(bind=connection)

    nested = connection.begin_nested()

    @event.listens_for(session.connection(), "after_transaction_end")
    def end_savepoint(conn, transaction):
        if transaction.nested and not transaction._parent.nested:
            session.expire_all()
            connection.begin_nested()

    db.session = session
    yield session

    session.close()
    transaction.rollback()
    connection.close()


@pytest.yield_fixture(scope='function')
def client(session):
    """A Flask test client. An instance of :class:`flask.testing.TestClient`
    by default.
    """
    with main.app.test_client() as client:
        yield client
