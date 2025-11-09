from flask import Flask, render_template_string, request, jsonify
from flask_socketio import SocketIO
import requests
import os
import logging

# Basic logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("presence_app")

# Configuration
ORIGINAL_APP = os.environ.get('ORIGINAL_APP', 'http://localhost:5000')
PRESENCE_PORT = int(os.environ.get('PRESENCE_PORT', 5001))

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# Ordered list of non-admin connected clients (sids) and mapping sid->name
connected_sids = []
sid_to_name = {}

# Host welcome page HTML (served at /)
START_HTML = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8"/>
    <title>InfiniteDraft — Host</title>
    <style>
      body { font-family: Arial, sans-serif; padding: 24px; background: #222; color: #eee; }
      .controls { display:flex; flex-direction:column; gap:12px; max-width:360px; }
      label { font-weight:600; }
      select, button { padding:8px 10px; font-size:14px; }
      #status { margin-top:12px; color:#bdbdbd; }
    </style>
  </head>
  <body>
    <h1>InfiniteDraft — Host</h1>
    <div class="controls">
      <div>
        <label for="setSelect">Set</label><br>
        <select id="setSelect"></select>
      </div>

      <div>
        <button id="goBtn">Go!</button>
      </div>

      <div id="status">Connected clients: <span id="count">0</span></div>
    </div>

    <script src="https://cdn.socket.io/4.5.3/socket.io.min.js"></script>
    <script>
      const socket = io(window.location.origin + '?source=admin', { transports: ['websocket', 'polling'] });
      socket.on('connect', () => console.log('socket connected to presence server (admin)'));
      socket.on('user_count', n => document.getElementById('count').textContent = n);
      socket.on('go', data => { console.log('Go event received (host page)', data); });
      socket.on('packs_update', data => { console.log('packs_update', data); });

      async function fetchSets() {
        try {
          const res = await fetch('/sets');
          const j = await res.json();
          const sel = document.getElementById('setSelect');
          sel.innerHTML = '';
          (j.sets || []).forEach(s => {
            const opt = document.createElement('option');
            opt.value = s;
            opt.textContent = s;
            sel.appendChild(opt);
          });
        } catch (e) {
          console.warn('Failed to load sets', e);
        }
      }

      document.getElementById('goBtn').addEventListener('click', async () => {
        const set = document.getElementById('setSelect').value || null;
        document.getElementById('status').textContent = 'Sending start to server…';
        try {
          const res = await fetch('/go', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ set })
          });
          const json = await res.json();
          if (!res.ok) {
            document.getElementById('status').textContent = 'Failed: ' + (json.error || res.status);
            return;
          }
          document.getElementById('status').textContent = 'Started — notified ' + (json.notified_clients || 0) + ' clients';
        } catch (err) {
          document.getElementById('status').textContent = 'Error: ' + err;
        }
      });

      fetchSets();
    </script>
  </body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(START_HTML)


@app.route('/sets')
def forward_sets():
    try:
        r = requests.get(f"{ORIGINAL_APP}/get_sets", timeout=5)
        if r.status_code == 200:
            return jsonify(r.json())
        log.warning("Original app /get_sets returned status %s: %s", r.status_code, r.text)
        return jsonify({'sets': []})
    except Exception as e:
        log.exception("Error fetching sets from original app: %s", e)
        return jsonify({'sets': []})


@app.route('/go', methods=['POST'])
def go():
    data = request.get_json() or {}
    set_name = data.get('set')

    players_to_use = len(connected_sids)
    if players_to_use <= 0:
        return jsonify({'error': 'No connected clients to start'}, 400)

    payload = {'players': players_to_use, 'rounds': 3}
    if set_name is not None:
        payload['set'] = set_name

    log.info("Host requested go; forwarding to ORIGINAL_APP /refresh with payload: %s", payload)

    try:
        r = requests.post(f"{ORIGINAL_APP}/refresh", json=payload, timeout=10)
        if r.status_code >= 400:
            log.error("Original app returned error status %s: %s", r.status_code, r.text)
            return jsonify({'error': 'Original app returned error', 'status': r.status_code}), 500
    except Exception as e:
        log.exception("Failed to reach original app: %s", e)
        return jsonify({'error': 'Failed to reach original app: ' + str(e)}), 500

    # Assign each connected client a starting pack index (0..players-1)
    notified = 0
    for i, sid in enumerate(list(connected_sids)):
        assigned_index = i % players_to_use
        name = sid_to_name.get(sid, '')
        try:
            socketio.emit('go', {'pack_index': assigned_index, 'name': name, 'players': players_to_use}, to=sid)
            notified += 1
        except Exception:
            log.exception("Failed to emit 'go' to sid=%s", sid)

    log.info("Go emitted to %s clients (connected=%s)", notified, len(connected_sids))
    return jsonify({'ok': True, 'notified_clients': notified})


@app.route('/notify', methods=['POST'])
def notify():
    """
    Endpoint for app.py to POST updates. This simply forwards the JSON payload to all connected clients
    as a 'packs_update' socket event.
    """
    data = request.get_json() or {}
    try:
        socketio.emit('packs_update', data)
        return jsonify({'ok': True})
    except Exception as e:
        log.exception("Failed to emit packs_update: %s", e)
        return jsonify({'ok': False, 'error': str(e)}), 500


@socketio.on('connect')
def on_connect():
    sid = request.sid
    src = request.args.get('source')
    name = request.args.get('name')
    log.info("Socket connect sid=%s source=%s name=%s", sid, src, name)

    if src == 'admin':
        socketio.emit('user_count', len(connected_sids), to=sid)
        return

    if not name:
        try:
            socketio.emit('error', {'error': 'name required'}, to=sid)
        except Exception:
            pass
        try:
            socketio.disconnect(sid)
        except Exception:
            pass
        return

    sid_to_name[sid] = name
    connected_sids.append(sid)
    socketio.emit('user_count', len(connected_sids))
    log.info("Client registered: sid=%s name=%s count=%s", sid, name, len(connected_sids))


@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    src = request.args.get('source')
    log.info("Socket disconnect sid=%s source=%s", sid, src)
    if src == 'admin':
        return

    if sid in connected_sids:
        connected_sids.remove(sid)
    if sid in sid_to_name:
        del sid_to_name[sid]
    socketio.emit('user_count', len(connected_sids))
    log.info("Client removed: sid=%s count=%s", sid, len(connected_sids))


if __name__ == '__main__':
    log.info("Starting presence_app on port %s, forwarding to ORIGINAL_APP=%s", PRESENCE_PORT, ORIGINAL_APP)
    socketio.run(app, host='0.0.0.0', port=PRESENCE_PORT)