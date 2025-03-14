--- a/
+++ b/
@@ -9,10 +9,13 @@
 from flask import redirect
 from flask import render_template
 from flask import request
 from flask import session
-from flask.ext.login import LoginManager
-from flask.ext.login import current_user
-from flask.ext.sqlalchemy import SQLAlchemy
+# Updated import paths - flask.ext.* syntax is deprecated
+from flask_login import LoginManager
+from flask_login import current_user
+from flask_sqlalchemy import SQLAlchemy
 from raven.contrib.flask import Sentry
+# Added for SQLAlchemy 2.0+ engine configuration
+from sqlalchemy import create_engine
 from user.current_user import CurrentUser
 from user.current_user import Anonymous
 
@@ -76,7 +79,8 @@
 Initialize mail
 
 def create_mail(app):
-    import flask.ext.mail
+    # Updated import path - flask.ext.* syntax is deprecated
+    import flask_mail as flask_mail
     mail = flask.ext.mail.Mail()
     mail.init_app(app)
     return mail