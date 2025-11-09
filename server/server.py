from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
from flask_cors import CORS
import os
import logging
import random
import csv
import copy

# Logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("server")

# Configuration
HOST_PORT = int(os.environ.get('HOST_PORT', 5001))

# Where repo-root static lives (used by Flask static paths)
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), 'templates')

app = Flask(__name__, template_folder=TEMPLATES_DIR, static_folder=os.path.join(REPO_ROOT, 'static'))

# enable CORS for HTTP endpoints (client UI runs on different origin)
CORS(app, resources={r"/*": {"origins": "*"}})

# Socket.IO with CORS allowed for sockets
socketio = SocketIO(app, cors_allowed_origins="*")

# Game configuration
PACK_SIZE = 14
DEFAULT_PLAYERS = 5
DEFAULT_ROUNDS = 3  # total rounds in a draft

# Global server-side game state
all_cards = []                # fallback card pool (loaded from CSV)
packs_rounds = []             # packs_rounds[round_index][pack_index] -> list of card dicts
packs_ready_rounds = []       # parallel boolean arrays
current_round = 0
TOTAL_ROUNDS = DEFAULT_ROUNDS

# If host selected a set with pack folders, we precompute a chosen list of pack folders
# sized players * TOTAL_ROUNDS. For each round we use the appropriate slice.
chosen_pack_folders = None
chosen_set_name = None

# Per-client decks by player name (authoritative)
decks_by_name = {}

# Presence tracking (ordered)
connected_sids = []
sid_to_name = {}

# Remember number of players used when last refresh() ran — helpful for diagnostics
players_count = None

BASE_DIR = REPO_ROOT  # used for loading sets/cards files


# -----------------------
# Helpers
# -----------------------
def load_cards_from_csv_path(file_path):
    cards = []
    try:
        with open(file_path, newline='', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                cards.append({'name': row.get('name', '').strip(), 'url': row.get('image_url', '').strip()})
    except FileNotFoundError:
        return []
    return cards


def load_cards_from_csv(filename='cards.csv'):
    file_path = os.path.join(BASE_DIR, filename)
    return load_cards_from_csv_path(file_path)


def _deepcopy_rounds(rounds_list):
    # Ensure we don't hold references back into source lists
    return [ [ copy.deepcopy(pack) for pack in round_packs ] for round_packs in rounds_list ]


def list_set_folders():
    sets_dir = os.path.join(BASE_DIR, 'sets')
    try:
        entries = os.listdir(sets_dir)
    except FileNotFoundError:
        return []
    folders = [e for e in entries if os.path.isdir(os.path.join(sets_dir, e))]
    return sorted(folders)


def list_pack_folders(set_name):
    set_dir = os.path.join(BASE_DIR, 'sets', set_name)
    try:
        entries = os.listdir(set_dir)
    except FileNotFoundError:
        return []
    folders = [e for e in entries if os.path.isdir(os.path.join(set_dir, e))]
    return sorted(folders)


def load_pack_cards(set_name, pack_folder):
    pack_csv_path = os.path.join(BASE_DIR, 'sets', set_name, pack_folder, 'cards.csv')
    return load_cards_from_csv_path(pack_csv_path)


def _current_round_snapshot():
    """Return a dict snapshot of the current round state for broadcasting to clients."""
    if not packs_rounds or current_round >= len(packs_rounds):
        packs = []
        ready = []
        counts = []
    else:
        packs = packs_rounds[current_round]
        ready = packs_ready_rounds[current_round]
        counts = [len(p) for p in packs]
    return {
        'current_round': current_round,
        'rounds': TOTAL_ROUNDS,
        'packs': packs,
        'packs_ready': ready,
        'packs_counts': counts,
        'players': players_count if players_count is not None else len(connected_sids)
    }


def notify_clients(payload):
    """
    Emit a 'packs_update' event to all connected socket clients.
    The payload will be augmented with the authoritative current-round snapshot.
    """
    try:
        snapshot = _current_round_snapshot()
        merged = {**snapshot, **payload}
        socketio.emit('packs_update', merged)
    except Exception as e:
        log.exception("Failed to emit packs_update: %s", e)


def generate_round_packs(num_players, round_index, set_name=None):
    """
    Generate and return a list of num_players packs for the specified round_index.
    If a set_name and chosen_pack_folders are available, use the corresponding chosen folders slice.
    Otherwise, generate fallback packs from all_cards (shuffled).
    This function does NOT append to packs_rounds; caller should append and initialize readiness.
    """
    packs = []
    if chosen_pack_folders and set_name:
        # Use the chosen list's slice for this round
        start = round_index * num_players
        group = chosen_pack_folders[start:start + num_players]
        for pf in group:
            cards = load_pack_cards(set_name, pf)
            packs.append(list(cards))
    else:
        # fallback: generate packs from all_cards shuffled per-round (allow cycling)
        shuffled = all_cards.copy()
        random.shuffle(shuffled)
        for p in range(num_players):
            pack = shuffled[:PACK_SIZE]
            if len(shuffled) >= PACK_SIZE:
                shuffled = shuffled[PACK_SIZE:]
            else:
                # cycle if not enough cards
                shuffled = shuffled + pack
            packs.append(list(pack))
    return packs


# -----------------------
# Initialization
# -----------------------
all_cards = load_cards_from_csv()


# -----------------------
# Server routes (game + host)
# -----------------------
@app.route('/')
def host_index():
    return render_template('index.html')


@app.route('/sets')
def forward_sets():
    try:
        folders = list_set_folders()
        return jsonify({'sets': folders})
    except Exception as e:
        log.exception("Error in /sets: %s", e)
        return jsonify({'sets': []})


@app.route('/go', methods=['POST'])
def host_go():
    """
    Host starts the draft. Body: { set: <name> (optional), rounds: <int> (optional) }.
    Uses current connected client count as players.
    This implementation will create only the first round initially; subsequent rounds will be generated
    when the server advances.
    """
    global current_round, TOTAL_ROUNDS, chosen_pack_folders, chosen_set_name, players_count, packs_rounds, packs_ready_rounds
    data = request.get_json() or {}
    set_name = data.get('set')
    rounds = data.get('rounds') or DEFAULT_ROUNDS

    players = len(connected_sids)
    if players <= 0:
        return jsonify({'error': 'No connected clients to start'}), 400

    TOTAL_ROUNDS = rounds
    players_count = players
    chosen_set_name = set_name
    chosen_pack_folders = None

    # If using a set, preselect folders for all rounds (kept for deterministic per-round slices)
    if set_name:
        available_pack_folders = list_pack_folders(set_name)
        if available_pack_folders:
            chosen = []
            shuffled = available_pack_folders.copy()
            random.shuffle(shuffled)
            i = 0
            total_needed = players * TOTAL_ROUNDS
            while len(chosen) < total_needed:
                chosen.append(shuffled[i % len(shuffled)])
                i += 1
            chosen_pack_folders = chosen

    # reset state: create only round 0 packs now
    packs_rounds = []
    packs_ready_rounds = []
    packs_rounds.append(generate_round_packs(players, 0, set_name=chosen_set_name))
    packs_ready_rounds.append([False] * players)
    # reset per-player decks
    for k in list(decks_by_name.keys()):
        decks_by_name[k] = []
    current_round = 0

    # notify clients of start and per-client starting indices
    notified = 0
    for i, sid in enumerate(list(connected_sids)):
        assigned_index = i % players
        name = sid_to_name.get(sid, '')
        try:
            socketio.emit('go', {'pack_index': assigned_index, 'name': name, 'players': players, 'rounds': TOTAL_ROUNDS}, to=sid)
            notified += 1
        except Exception:
            log.exception("Failed to emit 'go' to sid=%s", sid)

    log.info("host_go: created initial round packs (rounds=%s players=%s)", TOTAL_ROUNDS, players)
    log.info("packs_rounds sizes per round (so far): %s", [[len(p) for p in r] for r in packs_rounds])

    notify_clients({'event': 'refresh'})
    return jsonify({'ok': True, 'notified_clients': notified})


@app.route('/get_packs')
def get_packs():
    """
    Return packs for the current round plus readiness info and counts.
    """
    global current_round
    packs = packs_rounds[current_round] if packs_rounds and current_round < len(packs_rounds) else []
    ready = packs_ready_rounds[current_round] if packs_ready_rounds and current_round < len(packs_ready_rounds) else []
    log.debug("get_packs: current_round=%s packs_count=%s packs_ready=%s players_count=%s",
              current_round, len(packs), len(ready) if ready is not None else None, players_count)
    return jsonify({
        'packs': packs,
        'current_round': current_round,
        'rounds': TOTAL_ROUNDS,
        'packs_ready': ready,
        'packs_counts': [len(p) for p in packs]
    })


@app.route('/get_sets')
def get_sets():
    try:
        folders = list_set_folders()
        return jsonify({'sets': folders})
    except Exception as e:
        log.exception("Error in /get_sets: %s", e)
        return jsonify({'sets': []})


@app.route('/get_deck')
def get_deck():
    """
    Return the deck for a player name (used by client on startup/reconnect).
    GET ?name=<playername>
    """
    name = request.args.get('name')
    if not name:
        return jsonify({'deck': []})
    deck = decks_by_name.get(name, [])
    return jsonify({'deck': deck})


@app.route('/click', methods=['POST'])
def click():
    """
    User picks a card from a specified pack index (per-client mode).
    Accepts optional 'round' in request to avoid race if server advanced.
    Body: { player: <playerName>, card: <cardName>, pack_index: <int>, round: <int> (optional) }
    """
    global current_round, packs_rounds, packs_ready_rounds

    data = request.get_json() or {}
    player_name = data.get('player') or data.get('name')
    card_name = data.get('card') or data.get('card_name') or data.get('cardName')
    pack_index = data.get('pack_index')
    requested_round = data.get('round')

    if not player_name:
        return jsonify({'error': 'No player name provided'}), 400
    if not card_name:
        return jsonify({'error': 'No card name provided'}), 400
    if not packs_rounds:
        return jsonify({'error': 'No packs loaded'}, 400)

    # Choose round: prefer client's requested_round if valid and exists in packs_rounds, otherwise use authoritative current_round
    if isinstance(requested_round, int) and 0 <= requested_round < len(packs_rounds):
        target_round = requested_round
    else:
        target_round = current_round

    round_packs = packs_rounds[target_round]
    round_ready = packs_ready_rounds[target_round]

    if not isinstance(pack_index, int):
        return jsonify({'error': 'pack_index is required in multi-client mode'}), 400
    if pack_index < 0 or pack_index >= len(round_packs):
        return jsonify({'error': 'Invalid pack index'}), 400

    current_pack = round_packs[pack_index]
    card_index = next((i for i, c in enumerate(current_pack) if c['name'] == card_name), None)
    if card_index is None:
        log.warning("click: card not found. payload=%s target_round=%s pack_index=%s pack_size=%s", data, target_round, pack_index, len(current_pack))
        return jsonify({'error': 'Card not found in specified pack'}), 404

    # perform pick
    card = current_pack.pop(card_index)
    decks_by_name.setdefault(player_name, []).append(card)

    # mark this pack ready for passing
    round_ready[pack_index] = True

    # Only attempt to advance the authoritative current_round when this pick was in that round
    round_advanced = False
    if target_round == current_round:
        # If all packs in the authoritative round are empty, advance
        all_empty = all(len(p) == 0 for p in round_packs)
        if all_empty:
            # If we haven't created the next round yet, generate it now (until TOTAL_ROUNDS)
            next_round_index = current_round + 1
            if next_round_index < TOTAL_ROUNDS:
                # generate next round packs (append to packs_rounds/packs_ready_rounds)
                num_players = players_count if players_count is not None else len(connected_sids)
                next_round_packs = generate_round_packs(num_players, next_round_index, set_name=chosen_set_name)
                packs_rounds.append(next_round_packs)
                packs_ready_rounds.append([False] * num_players)
                # Now advance authoritative current_round
                current_round = next_round_index
                round_advanced = True
                notify_clients({'event': 'round_advanced'})
                log.info("Advanced to round %s (generated new round packs)", current_round)
                log.info("packs_rounds sizes after advance: %s", [[len(p) for p in r] for r in packs_rounds])
            else:
                # No more rounds after this one: draft ends
                notify_clients({'event': 'draft_complete'})
                log.info("Draft complete")
    # compute next pack index for this client in the target_round view
    next_index = (pack_index + 1) % len(round_packs) if round_packs else 0

    # decide advancement relative to the target_round
    can_advance = False
    if not round_advanced and round_ready[next_index]:
        round_ready[next_index] = False
        can_advance = True
    elif round_advanced and current_round < len(packs_rounds) and len(packs_rounds[current_round]) > 0:
        can_advance = True
        # when we advanced, compute the index in the new authoritative round for this player
        next_index = pack_index % len(packs_rounds[current_round])

    # Broadcast authoritative snapshot merged with event
    notify_clients({'event': 'pick_made'})

    player_deck = decks_by_name.get(player_name, [])
    if can_advance:
        return jsonify({
            'advanced': True,
            'next_pack_index': next_index,
            'round_advanced': round_advanced,
            'current_round': current_round,
            'deck': player_deck
        })
    else:
        return jsonify({
            'advanced': False,
            'waiting_on': next_index,
            'round_advanced': round_advanced,
            'current_round': current_round,
            'deck': player_deck
        })


@app.route('/claim_pack', methods=['POST'])
def claim_pack():
    """
    Atomically claim a ready pack in the specified round (optional).
    Body: { pack_index: <int>, name: <playerName>, round: <int> (optional) }
    """
    global packs_rounds, packs_ready_rounds, current_round
    data = request.get_json() or {}
    pack_index = data.get('pack_index')
    name = data.get('name')
    requested_round = data.get('round')

    if pack_index is None or not isinstance(pack_index, int):
        return jsonify({'error': 'pack_index required'}), 400
    if not packs_rounds:
        return jsonify({'error': 'no packs loaded'}, 400)

    # prefer client-supplied round if valid
    if isinstance(requested_round, int) and 0 <= requested_round < len(packs_rounds):
        target_round = requested_round
    else:
        target_round = current_round

    if pack_index < 0 or pack_index >= len(packs_rounds[target_round]):
        return jsonify({'error': 'invalid pack_index'}), 400
    if not packs_ready_rounds[target_round][pack_index]:
        log.info("claim_pack: pack not ready payload=%s target_round=%s", data, target_round)
        return jsonify({'ok': False, 'error': 'pack not ready'}), 409

    # claim
    packs_ready_rounds[target_round][pack_index] = False
    notify_clients({'event': 'pack_claimed'})

    player_deck = decks_by_name.get(name, [])
    return jsonify({'ok': True, 'pack_index': pack_index, 'pack': packs_rounds[target_round][pack_index],
                    'packs_ready': packs_ready_rounds[target_round], 'deck': player_deck})


@app.route('/refresh', methods=['POST'])
def refresh():
    """
    External refresh endpoint (can be called by host OR any admin tooling).
    Accepts JSON { set, players, rounds }.
    This implementation creates only the first round immediately; subsequent rounds are generated on advance.
    """
    data = request.get_json() or {}
    set_name = data.get('set')
    players = data.get('players')
    rounds = data.get('rounds') or DEFAULT_ROUNDS

    # Use same behavior as host_go but return packs for client convenience
    # Reuse host_go logic by delegating to do the same steps
    # Simplified: call host_go-like logic inline
    global current_round, TOTAL_ROUNDS, chosen_pack_folders, chosen_set_name, players_count, packs_rounds, packs_ready_rounds
    TOTAL_ROUNDS = rounds
    players_count = players if players and isinstance(players, int) and players > 0 else DEFAULT_PLAYERS
    chosen_set_name = set_name
    chosen_pack_folders = None

    if chosen_set_name:
        available_pack_folders = list_pack_folders(chosen_set_name)
        if available_pack_folders:
            chosen = []
            shuffled = available_pack_folders.copy()
            random.shuffle(shuffled)
            i = 0
            total_needed = players_count * TOTAL_ROUNDS
            while len(chosen) < total_needed:
                chosen.append(shuffled[i % len(shuffled)])
                i += 1
            chosen_pack_folders = chosen

    # reset and create first round
    packs_rounds = []
    packs_ready_rounds = []
    packs_rounds.append(generate_round_packs(players_count, 0, set_name=chosen_set_name))
    packs_ready_rounds.append([False] * players_count)

    # reset per-player decks
    for k in list(decks_by_name.keys()):
        decks_by_name[k] = []
    current_round = 0

    notify_clients({'event': 'refresh'})
    current_packs = packs_rounds[current_round] if packs_rounds else []
    current_ready = packs_ready_rounds[current_round] if packs_ready_rounds else []
    return jsonify({
        'packs': current_packs,
        'deck': [],  # generic response (clients request their own deck via /get_deck)
        'current_round': current_round,
        'rounds': TOTAL_ROUNDS,
        'packs_ready': current_ready
    })


# -----------------------
# Socket presence handlers
# -----------------------
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

    # initialize deck for name if missing (keeps deck across reconnects)
    decks_by_name.setdefault(name, [])

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
    log.info("Starting server on port %s (templates=%s)", HOST_PORT, TEMPLATES_DIR)
    socketio.run(app, host='0.0.0.0', port=HOST_PORT)