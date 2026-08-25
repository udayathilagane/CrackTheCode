"""WSGI entry point for deployment.

Gunicorn serves the `app` object below. Creating the SocketIO instance in
crackthecode.py already wraps app.wsgi_app with the Socket.IO middleware, so
importing the module is all that is needed to enable websocket handling.

Start command (Render / any host):
    gunicorn -w 1 --threads 100 -b 0.0.0.0:$PORT app:app

-w 1 is mandatory: game state lives in the in-memory `games` dict, so a second
worker process would give players in the same group a different game.
"""

from crackthecode import app, socketio  # noqa: F401

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)
