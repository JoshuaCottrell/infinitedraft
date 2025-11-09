# client/client.py (thin UI server)
from flask import Flask, render_template, request
import os
import logging

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# The client app is only responsible for serving templates (client/templates).
CLIENT_DIR = os.path.abspath(os.path.dirname(__file__))
TEMPLATES_DIR = os.path.join(CLIENT_DIR, 'templates')

# Static assets are served from the repo root 'static' directory by default.
REPO_ROOT = os.path.abspath(os.path.join(CLIENT_DIR, '..'))
STATIC_DIR = os.path.join(REPO_ROOT, 'static')

app = Flask(__name__, template_folder=TEMPLATES_DIR, static_folder=STATIC_DIR)

# The client UI will talk to the server (presence/game) for actual data and socket events.
# By default, the presence/game server is expected at:
PRESENCE_SERVER = os.environ.get('PRESENCE_SERVER', 'http://localhost:5001')
log.info("Client will use PRESENCE_SERVER=%s", PRESENCE_SERVER)


@app.route('/')
def index():
    # Waiting room / join page template (client/templates/index.html)
    # PRESENCE_SERVER is injected into the template so client JS can connect correctly.
    return render_template('index.html', PRESENCE_SERVER=PRESENCE_SERVER)


@app.route('/draft')
def draft():
    # Draft UI template (client/templates/draft.html)
    # PRESENCE_SERVER is injected into the template so client JS can connect correctly.
    return render_template('draft.html', PRESENCE_SERVER=PRESENCE_SERVER)


if __name__ == '__main__':
    # Run client UI on port 5000 by default (so it's easy to open in the browser)
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('CLIENT_PORT', 5000)))