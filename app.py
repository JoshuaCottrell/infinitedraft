from flask import Flask, render_template, request, jsonify
import csv
import random
import os

app = Flask(__name__)

PACK_SIZE = 14
NUM_PACKS = 5

# Global state
all_cards = []      # All cards loaded from CSV (fallback)
packs = []          # List of packs (each pack is a list of card dicts)
deck = []           # Cards in deck
current_pack_index = 0

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Load CSV from a given path (ordered)
def load_cards_from_csv_path(file_path):
    cards = []
    try:
        with open(file_path, newline='', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                # expecting 'name' and 'image_url' columns
                cards.append({'name': row.get('name', '').strip(), 'url': row.get('image_url', '').strip()})
    except FileNotFoundError:
        return []
    return cards

# Load CSV on startup (root cards.csv)
def load_cards_from_csv(filename='cards.csv'):
    file_path = os.path.join(BASE_DIR, filename)
    return load_cards_from_csv_path(file_path)

def generate_packs_from_all_cards():
    global packs
    shuffled = all_cards.copy()
    random.shuffle(shuffled)
    packs = []
    for _ in range(NUM_PACKS):
        pack = shuffled[:PACK_SIZE]
        shuffled = shuffled[PACK_SIZE:] + pack  # allow cycling if not enough cards
        packs.append(pack)

# Helper: list set folders
def list_set_folders():
    sets_dir = os.path.join(BASE_DIR, 'sets')
    try:
        entries = os.listdir(sets_dir)
    except FileNotFoundError:
        return []
    folders = [e for e in entries if os.path.isdir(os.path.join(sets_dir, e))]
    return sorted(folders)

# Helper: list pack folders inside a set
def list_pack_folders(set_name):
    set_dir = os.path.join(BASE_DIR, 'sets', set_name)
    try:
        entries = os.listdir(set_dir)
    except FileNotFoundError:
        return []
    folders = [e for e in entries if os.path.isdir(os.path.join(set_dir, e))]
    return sorted(folders)

# Helper: load a pack's cards.csv (in-order)
def load_pack_cards(set_name, pack_folder):
    pack_csv_path = os.path.join(BASE_DIR, 'sets', set_name, pack_folder, 'cards.csv')
    return load_cards_from_csv_path(pack_csv_path)

# Initial load (fallback)
all_cards = load_cards_from_csv()
generate_packs_from_all_cards()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/get_packs')
def get_packs():
    return jsonify({'packs': packs})

@app.route('/get_sets')
def get_sets():
    folders = list_set_folders()
    return jsonify({'sets': folders})

@app.route('/click', methods=['POST'])
def click():
    global current_pack_index, packs, deck
    data = request.get_json()
    name = data.get('name') if isinstance(data, dict) else None

    if not name:
        return jsonify({'error': 'No card name provided'}), 400

    if not packs:
        return jsonify({'error': 'No packs loaded'}), 400

    # Find card in current pack
    current_pack = packs[current_pack_index]
    card_index = next((i for i, c in enumerate(current_pack) if c['name'] == name), None)
    if card_index is None:
        return jsonify({'error': 'Card not found in current pack'}), 404

    # Move card to deck
    card = current_pack.pop(card_index)
    deck.append(card)

    # Move to next pack (use current number of packs)
    if packs:
        current_pack_index = (current_pack_index + 1) % len(packs)
    else:
        current_pack_index = 0

    return jsonify({
        'pack': packs[current_pack_index] if packs else [],
        'deck': deck
    })

@app.route('/refresh', methods=['POST'])
def refresh():
    """
    Accepts optional JSON body:
      { "set": "<set_name>", "players": <1-8> }
    Behavior:
      - If a valid set is specified and found under sets/, randomly choose (players * 3) pack folders
        from sets/<set>/ and load each pack's cards.csv in order (no shuffling) into packs.
      - If set is missing or invalid, fallback to previous behavior (generate random packs from root cards.csv).
    """
    global deck, current_pack_index, packs, all_cards, NUM_PACKS

    data = request.get_json() or {}
    set_name = data.get('set')
    players = data.get('players')

    # Determine target number of packs
    num_packs_to_load = None
    if players is not None:
        try:
            p = int(players)
            if p < 1:
                p = 1
            # no more than 8 players (optional clamp), adjust as desired
            if p > 8:
                p = 8
            num_packs_to_load = p * 3
        except (ValueError, TypeError):
            num_packs_to_load = None

    # If a set is provided and exists, load packs from it
    if set_name:
        available_pack_folders = list_pack_folders(set_name)
        if available_pack_folders and num_packs_to_load:
            # Choose packs randomly but without changing internal order of each pack file
            chosen = []
            shuffled = available_pack_folders.copy()
            random.shuffle(shuffled)
            # If not enough unique folders, cycle through shuffled list to reach desired count
            i = 0
            while len(chosen) < num_packs_to_load:
                chosen.append(shuffled[i % len(shuffled)])
                i += 1
            # Now load each chosen pack's cards.csv (in-order)
            new_packs = []
            for pf in chosen:
                cards = load_pack_cards(set_name, pf)
                new_packs.append(cards)
            packs = new_packs
        else:
            # fallback: if no pack folders found or players invalid, fallback to root behavior
            all_cards = load_cards_from_csv()
            generate_packs_from_all_cards()
    else:
        # No set specified: fallback to original behavior
        all_cards = load_cards_from_csv()
        generate_packs_from_all_cards()

    deck = []
    current_pack_index = 0

    return jsonify({'packs': packs, 'deck': deck})

if __name__ == '__main__':
    app.run(debug=True)