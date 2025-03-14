@@ -5,8 +5,9 @@ from flask import redirect
 from flask import render_template
 from flask import send_from_directory
 from flask import url_for
-from flask.ext.login import current_user
-from flask.ext.login import login_required
+# Updated import path from flask.ext.login to flask_login as per Flask 1.0+ recommendations
+from flask_login import current_user
+from flask_login import login_required
 from main import app
 
 @app.route('/')
@@ -26,6 +27,7 @@ def email():
 	user = current_user.user
 	from schema.listing import Listing
 	listing = Listing.query.get(6)
+	# Note: For SQLAlchemy 2.0+, consider using Session.get(Listing, 6) if you update the query pattern elsewhere
 	return render_template('emails/welcome.html', listing=listing, user=user)
 
 