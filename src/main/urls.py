from main import app

# Import views to register in global 'app' space
import user.views
import auth.views
import main.views
import listing.views
import payment.views
import activity.views

# SQLAlchemy 2.0 and later no longer require explicit imports of views for registration.
# The application structure should be adjusted to reflect this change if applicable.
# For example, using blueprints or other means of registering routes.
# No direct code changes are needed here, but a broader review of route registration is necessary.
