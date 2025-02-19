from main import db
import schema


# FIXME: this should probably go away in favor of auth.controller.register
def test_new_user(session):
    user = schema.user.User(slug="testmeN", email="testmeN@hello.net", password="password", salt="salt")
    session.add(user)
    session.commit()

    u2 = session.query(schema.user.User).filter_by(slug="testmeN").first()
    assert user.slug == u2.slug
