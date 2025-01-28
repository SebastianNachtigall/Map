from flask import Flask, render_template, request, jsonify, Response
import json
import os
import queue
import threading
import time
import uuid
import logging
from collections import deque
from threading import Lock
import requests
from datetime import datetime

app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Directory to store individual pin files
PINS_DIR = 'pins'
os.makedirs(PINS_DIR, exist_ok=True)

# Cache for reverse geocoding and pins
location_cache = {}
pins_cache = {}  # In-memory cache for pins
last_cache_update = 0
CACHE_DURATION = 60  # Seconds before refreshing cache

def get_nearest_city(lat, lon):
    # Check cache first
    cache_key = f"{lat},{lon}"
    if cache_key in location_cache:
        return location_cache[cache_key]
    
    # Add a small delay to respect Nominatim's usage policy
    time.sleep(1)
    
    try:
        # Using Nominatim for reverse geocoding
        url = f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json&zoom=10"
        headers = {
            'User-Agent': 'TUI Map Application/1.0'
        }
        logger.info(f"Geocoding request for {lat}, {lon}")
        response = requests.get(url, headers=headers, timeout=5)
        response.raise_for_status()  # Raise exception for bad status codes
        data = response.json()
        logger.info(f"Geocoding response: {data}")
        
        # Extract city or nearest named place
        location = None
        address = data.get('address', {})
        
        # Try different address components in order of preference
        for key in ['city', 'town', 'village', 'suburb', 'county', 'state']:
            if key in address:
                location = address[key]
                logger.info(f"Found location {location} using key {key}")
                break
        
        if not location and 'display_name' in data:
            # Fallback to first part of display name
            location = data['display_name'].split(',')[0]
            logger.info(f"Using fallback location: {location}")
        
        # Cache the result
        location = location or "Unknown location"
        location_cache[cache_key] = location
        return location
        
    except Exception as e:
        logger.error(f"Error in reverse geocoding: {e}")
        return "Unknown location"

# Broadcast system for SSE
class Broadcaster:
    def __init__(self):
        self.clients = set()
        self.messages = deque(maxlen=100)  # Keep last 100 messages
        self.lock = Lock()
    
    def register(self, queue):
        with self.lock:
            self.clients.add(queue)
            # Send recent messages to new client
            for msg in self.messages:
                queue.put(msg)
        logger.info(f"Client registered. Total clients: {len(self.clients)}")
    
    def unregister(self, queue):
        with self.lock:
            self.clients.remove(queue)
        logger.info(f"Client unregistered. Total clients: {len(self.clients)}")
    
    def broadcast(self, msg):
        with self.lock:
            self.messages.append(msg)
            dead_clients = set()
            for client_queue in self.clients:
                try:
                    client_queue.put(msg)
                except:
                    dead_clients.add(client_queue)
            # Clean up dead clients
            for dead in dead_clients:
                self.clients.remove(dead)
        logger.info(f"Broadcasted message to {len(self.clients)} clients")

broadcaster = Broadcaster()

def load_locations():
    """Load all locations from the pins directory with caching"""
    global last_cache_update, pins_cache
    
    locations = []
    try:
        # Get list of files in pins directory
        files = os.listdir(PINS_DIR)
        logger.info(f"Found {len(files)} files in pins directory")
        
        for filename in files:
            if filename.endswith('.json'):
                try:
                    filepath = os.path.join(PINS_DIR, filename)
                    with open(filepath, 'r') as f:
                        location = json.load(f)
                        logger.debug(f"Loaded pin from {filename}: {location}")
                        locations.append(location)
                except Exception as e:
                    logger.error(f"Error loading pin file {filename}: {e}")
    except Exception as e:
        logger.error(f"Error loading locations: {e}")
    
    logger.info(f"Returning {len(locations)} locations")
    return locations

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/pins', methods=['GET', 'POST'])
def handle_pins():
    if request.method == 'GET':
        logger.info("GET request received for pins")
        return jsonify(load_locations())
    
    elif request.method == 'POST':
        try:
            pin_data = request.json
            pin_data['id'] = str(uuid.uuid4())  # Add unique ID
            pin_data['timestamp'] = datetime.now().isoformat()
            
            # Get location name
            location = get_nearest_city(pin_data['lat'], pin_data['lng'])
            pin_data['location'] = location
            
            # Save to file with ID as filename
            pin_file = os.path.join(PINS_DIR, f"{pin_data['id']}.json")
            with open(pin_file, 'w') as f:
                json.dump(pin_data, f)
            
            logger.info(f"Pin created: {pin_data}")
            return jsonify({'status': 'success', 'pin_data': pin_data})
            
        except Exception as e:
            logger.error(f"Error handling pin creation: {e}")
            return jsonify({'error': str(e)}), 400

@app.route('/pins/<pin_id>', methods=['DELETE'])
def delete_pin(pin_id):
    try:
        pin_file = os.path.join(PINS_DIR, f"{pin_id}.json")
        if os.path.exists(pin_file):
            os.remove(pin_file)
            logger.info(f"Pin deleted: {pin_id}")
            return jsonify({'status': 'success', 'message': 'Pin deleted successfully'})
        else:
            logger.warning(f"Pin not found: {pin_id}")
            return jsonify({'status': 'error', 'message': 'Pin not found'}), 404
    except Exception as e:
        logger.error(f"Error deleting pin: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/stream')
def stream():
    def event_stream():
        # Create a queue for this client
        client_queue = queue.Queue()
        client_id = str(uuid.uuid4())
        logger.info(f"New client connected: {client_id}")
        
        try:
            # Register client
            broadcaster.register(client_queue)
            
            while True:
                # Get message from queue
                message = client_queue.get()
                
                if message is None:  # Shutdown signal
                    break
                    
                # Send message to client
                yield f"data: {message}\n\n"
                
        except GeneratorExit:
            logger.info(f"Client disconnected: {client_id}")
        finally:
            broadcaster.unregister(client_queue)
            client_queue.put(None)  # Signal to exit
    
    return Response(event_stream(), mimetype='text/event-stream')

if __name__ == '__main__':
    app.run(debug=False)

    # app.run(debug=True, port=5005, threaded=True)
