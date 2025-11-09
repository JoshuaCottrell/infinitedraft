import requests
import csv
import time
import os

def get_scryfall_image_url(card_name):
    """Fetch the PNG image URL for a given Magic card from Scryfall."""
    url = f"https://api.scryfall.com/cards/named?exact={card_name}"
    response = requests.get(url)
    if response.status_code != 200:
        print(f"Error fetching {card_name}: {response.status_code}")
        return None

    data = response.json()
    # Handle double-faced or special cards
    if "image_uris" in data:
        return data["image_uris"]["png"]
    elif "card_faces" in data and "image_uris" in data["card_faces"][0]:
        return data["card_faces"][0]["image_uris"]["png"]
    else:
        print(f"No image found for {card_name}")
        return None


def generate_card_links(input_file, output_file=os.path.join(os.path.dirname(__file__), 'images.csv')):
    """Read card names from a file and write card name + PNG link to CSV."""
    with open(input_file, "r", encoding="utf-8") as f:
        card_names = [line.strip() for line in f if line.strip()]

    results = []
    for name in card_names:
        print(f"Fetching: {name}")
        image_url = get_scryfall_image_url(name)
        results.append({"name": name, "image_url": image_url})
        time.sleep(0.1)  # be nice to Scryfall’s API (rate limit ~10 req/s)

    # Write results to CSV
    with open(output_file, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=["name", "image_url"])
        writer.writeheader()
        writer.writerows(results)

    print(f"Done! Results written to {output_file}")


if __name__ == "__main__":
    input_path = os.path.join(os.path.dirname(__file__), 'cards.txt')
    generate_card_links(input_path)
