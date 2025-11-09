from flask import Flask, render_template, request, jsonify
import csv
import random
import os

app = Flask(__name__)

PACK_SIZE = 14
NUM_PACKS = 5

# Global state
all_cards = []      # All cards loaded from CSV
packs = []          # List of packs (each pack is a list of card dicts)
deck = []           # Cards in deck
current_pack_index = 0

# Load CSV on startup
def load_cards_from_csv(filename='cards.csv'):
    # Ensure we get the path relative to this script file
    base_dir = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(base_dir, filename)

    cards = []
    with open(file_path, newline='', encoding='utf-8') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            cards.append({'name': row['name'], 'url': row['image_url']})
    return cards

def generate_packs():
    global packs
    shuffled = all_cards.copy()
    random.shuffle(shuffled)
    packs = []
    for _ in range(NUM_PACKS):
        pack = shuffled[:PACK_SIZE]
        shuffled = shuffled[PACK_SIZE:] + pack  # allow cycling if not enough cards
        packs.append(pack)

# Initial load
all_cards = load_cards_from_csv()
generate_packs()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/get_packs')
def get_packs():
    # Return all packs as JSON
    return jsonify({'packs': packs})

@app.route('/click', methods=['POST'])
def click():
    global current_pack_index, packs, deck
    data = request.get_json()
    name = data.get('name')

    if not name:
        return jsonify({'error': 'No card name provided'}), 400

    # Find card in current pack
    current_pack = packs[current_pack_index]
    card_index = next((i for i, c in enumerate(current_pack) if c['name'] == name), None)
    if card_index is None:
        return jsonify({'error': 'Card not found in current pack'}), 404

    # Move card to deck
    card = current_pack.pop(card_index)
    deck.append(card)

    # Move to next pack
    current_pack_index = (current_pack_index + 1) % NUM_PACKS

    return jsonify({
        'pack': packs[current_pack_index],
        'deck': deck
    })

@app.route('/refresh', methods=['POST'])
def refresh():
    global deck, current_pack_index
    generate_packs()
    deck = []
    current_pack_index = 0
    return jsonify({'packs': packs, 'deck': deck})

if __name__ == '__main__':
    app.run(debug=True)
