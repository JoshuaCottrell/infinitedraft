from flask import Flask, render_template, request, jsonify
import csv
import random
import os
import logging
import requests

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Set BASE_DIR to repo root (one directory above this client/ folder)
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
log.info("client BASE_DIR set to %s", BASE_DIR)

# Configure Flask to use templates and static from repo root (templates/ and static/)
app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), 'templates'),
    static_folder=os.path.join(BASE_DIR, 'static')
)

PACK_SIZE = 14
NUM_PACKS = 5
ROUNDS = 3  # number of rounds

# Presence server notify URL (env override allowed)
PRESENCE_NOTIFY_URL = os.environ.get('PRESENCE_NOTIFY_URL', 'http://localhost:5001/notify')
log.info("PRESENCE_NOTIFY_URL set to %s", PRESENCE_NOTIFY_URL)

# Global state
all_cards = []      # All cards loaded from CSV (fallback)
# packs are organized by rounds: packs_rounds[round_index][pack_index] -> list of card dicts
packs_rounds = []   # list of rounds, each round is list of packs
packs_ready_rounds = []  # parallel boolean arrays for readiness per pack per round
current_round = 0
deck = []           # Cards in deck (global)
# NOTE: per-client pack_index flows use pack_index parameter in requests


# -----------------------
# CSV / pack helpers
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


def generate_packs_fallback(num_players, rounds=ROUNDS):
    """
    Generate `num_players` packs for each round (rounds total) from all_cards (shuffled).
    Initialize packs_ready accordingly.
    """
    global packs_rounds, packs_ready_rounds
    shuffled = all_cards.copy()
    random.shuffle(shuffled)
    packs_rounds = []
    packs_ready_rounds = []
    for r in range(rounds):
        round_packs = []
        for p in range(num_players):
            pack = shuffled[:PACK_SIZE]
            if len(shuffled) >= PACK_SIZE:
                shuffled = shuffled[PACK_SIZE:]
            else:
                # cycle if not enough cards
                shuffled = shuffled + pack
            round_packs.append(pack)
        packs_rounds.append(round_packs)
        packs_ready_rounds.append([False] * num_players)


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


def notify_presence(payload):
    """
    Notify presence server so it can emit realtime updates to connected clients.
    Best-effort; failures are logged but do not block primary flow.
    """
    try:
        requests.post(PRESENCE_NOTIFY_URL, json=payload, timeout=2)
    except Exception as e:
        log.debug("Failed to notify presence server: %s", e)


# -----------------------
# Initial load
# -----------------------
all_cards = load_cards_from_csv()
# default fallback initial packs
generate_packs_fallback(NUM_PACKS, rounds=ROUNDS)
current_round = 0
deck = []


# -----------------------
# Routes
# -----------------------
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/draft')
def draft():
    return render_template('draft.html')


@app.route('/get_packs')
def get_packs():
    """
    Return the packs for the current round and readiness state.
    """
    global packs_rounds, packs_ready_rounds, current_round
    packs = packs_rounds[current_round] if packs_rounds and current_round < len(packs_rounds) else []
    ready = packs_ready_rounds[current_round] if packs_ready_rounds and current_round < len(packs_ready_rounds) else []
    return jsonify({
        'packs': packs,
        'current_round': current_round,
        'rounds': len(packs_rounds),
        'packs_ready': ready
    })


@app.route('/get_sets')
def get_sets():
    try:
        folders = list_set_folders()
        return jsonify({'sets': folders})
    except Exception as e:
        log.exception("Error in /get_sets: %s", e)
        return jsonify({'sets': []})


@app.route('/click', methods=['POST'])
def click():
    """
    Pick card from a specific pack in the current round (pack_index required for per-client mode),
    or use global behavior if pack_index is omitted.
    """
    global packs_rounds, packs_ready_rounds, current_round, deck

    data = request.get_json() or {}
    name = data.get('name')
    pack_index = data.get('pack_index')

    if not name:
        return jsonify({'error': 'No card name provided'}), 400
    if not packs_rounds or current_round >= len(packs_rounds):
        return jsonify({'error': 'No packs loaded'}, 400)

    round_packs = packs_rounds[current_round]
    round_ready = packs_ready_rounds[current_round]

    # determine target pack index
    if isinstance(pack_index, int):
        if pack_index < 0 or pack_index >= len(round_packs):
            return jsonify({'error': 'Invalid pack index'}), 400
        target_pack_index = pack_index
    else:
        # backward compatibility not used in per-client multi-user flow: default to 0
        target_pack_index = 0

    current_pack = round_packs[target_pack_index]
    card_index = next((i for i, c in enumerate(current_pack) if c['name'] == name), None)
    if card_index is None:
        return jsonify({'error': 'Card not found in specified pack'}), 404

    # remove card and append to deck
    card = current_pack.pop(card_index)
    deck.append(card)

    # mark this pack as ready
    round_ready[target_pack_index] = True

    # check if this round is fully exhausted (all packs empty) => advance round
    all_empty = all(len(p) == 0 for p in round_packs)
    round_advanced = False
    if all_empty:
        # advance to next round if available
        if current_round + 1 < len(packs_rounds):
            current_round += 1
            round_advanced = True
            notify_presence({
                'event': 'round_advanced',
                'current_round': current_round,
                'rounds': len(packs_rounds)
            })
        else:
            notify_presence({
                'event': 'draft_complete'
            })

    # compute next pack index for the client (circular)
    next_index = (target_pack_index + 1) % len(round_packs)

    # if the next pack is ready (in the round that client is acting on), allow immediate advance
    can_advance = False
    if not round_advanced:
        if round_ready[next_index]:
            round_ready[next_index] = False
            can_advance = True
    else:
        if current_round < len(packs_rounds):
            can_advance = True
            next_index = target_pack_index % len(packs_rounds[current_round])

    notify_presence({
        'event': 'pick_made',
        'round': current_round,
        'packs_ready': packs_ready_rounds[current_round] if current_round < len(packs_ready_rounds) else [],
        'packs_counts': [len(p) for p in packs_rounds[current_round]] if current_round < len(packs_rounds) else []
    })

    if can_advance:
        return jsonify({
            'advanced': True,
            'next_pack_index': next_index,
            'round_advanced': round_advanced,
            'current_round': current_round,
            'deck': deck
        })
    else:
        return jsonify({
            'advanced': False,
            'waiting_on': next_index,
            'round_advanced': round_advanced,
            'current_round': current_round,
            'deck': deck
        })


@app.route('/claim_pack', methods=['POST'])
def claim_pack():
    """
    Claim a ready pack for the current round.
    Body: { "pack_index": <int> }
    """
    global packs_rounds, packs_ready_rounds, current_round
    data = request.get_json() or {}
    pack_index = data.get('pack_index')
    if pack_index is None or not isinstance(pack_index, int):
        return jsonify({'error': 'pack_index required'}), 400
    if not packs_rounds or current_round >= len(packs_rounds):
        return jsonify({'error': 'no packs loaded'}, 400)

    if pack_index < 0 or pack_index >= len(packs_rounds[current_round]):
        return jsonify({'error': 'invalid pack_index'}), 400

    if not packs_ready_rounds[current_round][pack_index]:
        return jsonify({'ok': False, 'error': 'pack not ready'}), 409

    # mark as not ready (claimed)
    packs_ready_rounds[current_round][pack_index] = False
    notify_presence({
        'event': 'pack_claimed',
        'round': current_round,
        'pack_index': pack_index
    })
    return jsonify({'ok': True, 'pack_index': pack_index, 'pack': packs_rounds[current_round][pack_index],
                    'packs_ready': packs_ready_rounds[current_round]})


@app.route('/refresh', methods=['POST'])
def refresh():
    """
    Accepts optional JSON body:
      { "set": "<set_name>", "players": <int>, "rounds": <int> }
    """
    global deck, current_round, packs_rounds, packs_ready_rounds, all_cards

    data = request.get_json() or {}
    set_name = data.get('set')
    players = data.get('players')
    rounds = data.get('rounds') or ROUNDS

    # Determine number of players
    num_players = None
    if players is not None:
        try:
            num_players = int(players)
            if num_players < 1:
                num_players = 1
            if num_players > 128:
                num_players = 128
        except (ValueError, TypeError):
            num_players = None

    if not num_players:
        num_players = NUM_PACKS  # fallback to default

    # If a set is provided and exists, load packs for each round from pack folders
    if set_name:
        available_pack_folders = list_pack_folders(set_name)
        if available_pack_folders:
            chosen = []
            shuffled = available_pack_folders.copy()
            random.shuffle(shuffled)
            i = 0
            total_needed = num_players * rounds
            while len(chosen) < total_needed:
                chosen.append(shuffled[i % len(shuffled)])
                i += 1
            new_packs_rounds = []
            for r in range(rounds):
                start = r * num_players
                group = chosen[start:start + num_players]
                round_packs = []
                for pf in group:
                    cards = load_pack_cards(set_name, pf)
                    round_packs.append(cards)
                new_packs_rounds.append(round_packs)
            packs_rounds = new_packs_rounds
            packs_ready_rounds = [[False] * num_players for _ in range(rounds)]
        else:
            all_cards = load_cards_from_csv()
            generate_packs_fallback(num_players, rounds=rounds)
    else:
        all_cards = load_cards_from_csv()
        generate_packs_fallback(num_players, rounds=rounds)

    deck = []
    current_round = 0

    notify_presence({
        'event': 'refresh',
        'rounds': len(packs_rounds),
        'current_round': current_round,
        'packs_counts': [ [len(p) for p in r] for r in packs_rounds ]
    })

    current_packs = packs_rounds[current_round] if packs_rounds else []
    current_ready = packs_ready_rounds[current_round] if packs_ready_rounds else []
    return jsonify({
        'packs': current_packs,
        'deck': deck,
        'current_round': current_round,
        'rounds': len(packs_rounds),
        'packs_ready': current_ready
    })


if __name__ == '__main__':
    app.run(debug=True)