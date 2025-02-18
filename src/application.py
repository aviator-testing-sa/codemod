#!/usr/bin/env python
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

if __name__ == '__main__':
    os.environ['APP_CONFIG_FILE'] = '../config/dev.cfg'
else:
    os.environ['APP_CONFIG_FILE'] = '../config/prod.cfg'

from main import app

application = app
app.secret_key = app.config['APP_SECRET']

# SQLAlchemy 2.x style engine creation
engine = create_engine(app.config['SQLALCHEMY_DATABASE_URI'])  # Ensure you have this config
Session = sessionmaker(bind=engine)
app.db_session = Session  # Attach the session to the app context

if __name__ == '__main__':
    @app.before_first_request
    def debug_mailserver():
        import mailserver
        app.debug_mailserver = mailserver.debug_server(app.config['MAIL_SERVER'], app.config['MAIL_PORT'])

    # FIXME: flask forks itself as a child process meaning it gets called twice
    #        rea
#    @atexit.register
#    def debug_mailserver_cleanup():
#        pass
#        if hasattr(app, 'debug_mailserver'):
#            app.debug_mailserver.close()


    app.run(debug=True)
