import contextlib
import os
import pytest
import main
import sqlalchemy


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


# Changed from pytest.yield_fixture to pytest.fixture with yield
@pytest.fixture(scope='session')
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


# Changed from pytest.yield_fixture to pytest.fixture with yield
@pytest.fixture(scope='function')
def session(db, request):
    """Creates a new database session for a test."""
    connection = db.engine.connect()
    # In SQLAlchemy 2.0, begin() returns a transaction object directly
    transaction = connection.begin()

    # Updated for SQLAlchemy 2.0+ session creation pattern
    options = dict(bind=connection, binds={})
    session = db.create_scoped_session(options=options)

    session.begin_nested()

    # Updated event listener pattern for SQLAlchemy 2.0+
    # session is actually a scoped_session
    # for the `after_transaction_end` event, we need a session instance to
    # listen for, hence the `session()` call
    @sqlalchemy.event.listens_for(session(), 'after_transaction_end')
    def restart_savepoint(sess, trans):
        # Check if we have nested transaction and not a top-level transaction
        if trans.nested and not trans._parent.nested:
            session.expire_all()
            session.begin_nested()

    db.session = session

    yield session

    # Proper teardown in SQLAlchemy 2.0+
    session.remove()
    transaction.rollback()
    connection.close()


# Changed from pytest.yield_fixture to pytest.fixture with yield
@pytest.fixture(scope='function')
def client(session):
    """A Flask test client. An instance of :class:`flask.testing.TestClient`
    by default.
    """
    with main.app.test_client() as client:
        yield client