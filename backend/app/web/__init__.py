"""The HTTP layer: one router per area of the site, plugged into the app in app/main.py.

Routers only translate HTTP into calls to app.services (and back); the business rules live there.
"""
