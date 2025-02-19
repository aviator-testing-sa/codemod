#!/usr/bin/env python
import os

if __name__ == '__main__':
    os.environ['APP_CONFIG_FILE'] = '../config/dev.cfg'
else:
    os.environ['APP_CONFIG_FILE'] = '../config/prod.cfg'

from main import app

application = app
app.config['SECRET_KEY'] = app.config['APP_SECRET']


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
