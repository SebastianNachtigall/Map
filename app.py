from flask import Flask, render_template, request, jsonify, Response, make_response
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

@app.route('/generate-light-map')
def generate_light_map():
    # Get all pins
    pins = []
    if os.path.exists(PINS_DIR):
        for pin_file in os.listdir(PINS_DIR):
            if pin_file.endswith('.json'):
                with open(os.path.join(PINS_DIR, pin_file), 'r') as f:
                    pin = json.load(f)
                    pins.append(pin)
    
    # Sort pins by timestamp
    pins.sort(key=lambda x: x.get('timestamp', ''))
    
    # Generate HTML template
    html_template = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TUI Map Snapshot</title>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        body {
            margin: 0;
            padding: 0;
            font-family: Arial, sans-serif;
            height: 100vh;
            display: flex;
            flex-direction: column;
        }
        
        .header {
            background-color: white;
            padding: 10px 20px;
            display: flex;
            align-items: center;
            gap: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        
        .header h1 {
            margin: 0;
            font-size: 24px;
            color: #333;
        }
        
        #map {
            flex: 1;
            position: relative;
        }
        
        .connection-heart {
            font-size: 14px;
            position: absolute;
            transform-origin: center;
            pointer-events: none;
            text-shadow: 0 0 3px white;
            z-index: 1001;
        }
        
        @supports (offset-path: path('M 0 0 L 100 100')) {
            .connection-heart {
                animation: moveHeart 4s linear infinite;
            }
            
            @keyframes moveHeart {
                0% {
                    offset-distance: 0%;
                    offset-rotate: 0deg;
                }
                50% {
                    offset-distance: 100%;
                    offset-rotate: 0deg;
                }
                100% {
                    offset-distance: 0%;
                    offset-rotate: 0deg;
                }
            }
        }
        
        .connect-btn {
            position: absolute;
            bottom: 20px;
            right: 20px;
            z-index: 1000;
            background: white;
            border: 2px solid rgba(0,0,0,0.2);
            border-radius: 4px;
            padding: 8px 12px;
            font-size: 16px;
            cursor: pointer;
            box-shadow: 0 1px 5px rgba(0,0,0,0.4);
            transition: all 0.2s ease;
        }
        
        .connect-btn:hover {
            background: #f4f4f4;
            transform: translateY(-1px);
            box-shadow: 0 2px 6px rgba(0,0,0,0.4);
        }
        
        .connect-btn.active {
            background: #e8e8e8;
            transform: translateY(1px);
            box-shadow: 0 1px 3px rgba(0,0,0,0.3);
        }
        
        .leaflet-popup-content {
            text-align: center;
        }
        
        .popup-image {
            max-width: 200px;
            max-height: 200px;
            margin: 10px 0;
            border-radius: 8px;
        }
        
        .marker-label {
            background: rgba(255, 255, 255, 0.9);
            border: 1px solid #ccc;
            border-radius: 4px;
            padding: 2px 6px;
            font-size: 12px;
            white-space: nowrap;
            pointer-events: auto;
            cursor: pointer;
            transition: all 0.2s ease;
            transform: translate(-50%, -10px);
            box-shadow: 0 1px 3px rgba(0,0,0,0.2);
        }
        
        .marker-label:hover {
            background: white;
            transform: translate(-50%, -12px);
            box-shadow: 0 2px 4px rgba(0,0,0,0.3);
        }
        
        .leaflet-popup {
            min-width: 220px;
        }
        
        .leaflet-popup-content {
            text-align: center;
            width: auto !important;
            min-width: 200px;
        }
        
        .popup-image {
            max-width: 200px;
            max-height: 200px;
            margin: 10px 0;
            border-radius: 8px;
            display: block;
            margin-left: auto;
            margin-right: auto;
        }
    </style>
</head>
<body>
    <div class="header">
        <img src="https://logos-world.net/wp-content/uploads/2024/07/TUI-Symbol.png" alt="TUI Logo" style="height: 40px;">
        <h1>People of TUI.com</h1>
    </div>
    
    <div id="map">
        <button id="connectBtn" class="connect-btn" title="Toggle pin connections">❤️</button>
    </div>

    <script>
        // Initialize map
        const map = L.map('map').setView([50.0, 15.0], 4);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            maxZoom: 19,
            attribution: 'OpenStreetMap contributors'
        }).addTo(map);

        // Initialize variables
        const markers = [];
        let connectionLines = [];
        let heartElements = [];
        let showConnections = false;

        // Create red icon for markers
        const redIcon = L.icon({
            iconUrl: 'https://raw.githubusercontent.com/pointhi/leaflet-color-markers/master/img/marker-icon-2x-red.png',
            shadowUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-shadow.png',
            iconSize: [25, 41],
            iconAnchor: [12, 41],
            popupAnchor: [1, -34],
            shadowSize: [41, 41]
        });

        // Function to create curved line
        function createCurvedLine(p1, p2) {
            const controlPoint = [
                (p1.lat + p2.lat) / 2 + (p2.lng - p1.lng) * 0.1,
                (p1.lng + p2.lng) / 2 - (p2.lat - p1.lat) * 0.1
            ];
            
            const curvePoints = [];
            for (let t = 0; t <= 1; t += 0.1) {
                const lat = Math.pow(1-t, 2) * p1.lat + 
                           2 * (1-t) * t * controlPoint[0] + 
                           Math.pow(t, 2) * p2.lat;
                const lng = Math.pow(1-t, 2) * p1.lng + 
                           2 * (1-t) * t * controlPoint[1] + 
                           Math.pow(t, 2) * p2.lng;
                curvePoints.push([lat, lng]);
            }
            
            const line = L.polyline(curvePoints, {
                color: '#666',
                weight: 1,
                dashArray: '5, 10',
                opacity: 0.6,
                className: 'connection-line'
            });
            
            const pathData = curvePoints.reduce((acc, point, i) => {
                const pixel = map.latLngToLayerPoint(L.latLng(point[0], point[1]));
                return acc + (i === 0 ? `M ${pixel.x} ${pixel.y}` : ` L ${pixel.x} ${pixel.y}`);
            }, '');
            
            const heart = document.createElement('div');
            heart.className = 'connection-heart';
            heart.textContent = '❤️';
            heart.style.left = '0';
            heart.style.top = '0';
            
            if (CSS.supports('offset-path', `path('${pathData}')`)) {
                heart.style.offsetPath = `path('${pathData}')`;
            } else {
                const start = map.latLngToLayerPoint(L.latLng(p1.lat, p1.lng));
                heart.style.left = `${start.x}px`;
                heart.style.top = `${start.y}px`;
            }
            
            const mapContainer = document.querySelector('.leaflet-map-pane');
            mapContainer.appendChild(heart);
            heartElements.push(heart);
            
            const updateHeartPath = () => {
                const newPathData = curvePoints.reduce((acc, point, i) => {
                    const pixel = map.latLngToLayerPoint(L.latLng(point[0], point[1]));
                    return acc + (i === 0 ? `M ${pixel.x} ${pixel.y}` : ` L ${pixel.x} ${pixel.y}`);
                }, '');
                
                if (CSS.supports('offset-path', `path('${newPathData}')`)) {
                    heart.style.offsetPath = `path('${newPathData}')`;
                } else {
                    const start = map.latLngToLayerPoint(L.latLng(p1.lat, p1.lng));
                    heart.style.left = `${start.x}px`;
                    heart.style.top = `${start.y}px`;
                }
            };
            
            map.on('move', updateHeartPath);
            map.on('zoom', updateHeartPath);
            
            return line;
        }

        // Function to toggle connections
        function toggleConnections() {
            showConnections = !showConnections;
            
            connectionLines.forEach(line => map.removeLayer(line));
            connectionLines = [];
            heartElements.forEach(heart => heart.remove());
            heartElements = [];
            
            if (showConnections) {
                for (let i = 0; i < markers.length; i++) {
                    for (let j = i + 1; j < markers.length; j++) {
                        const line = createCurvedLine(
                            markers[i].getLatLng(),
                            markers[j].getLatLng()
                        );
                        line.addTo(map);
                        connectionLines.push(line);
                    }
                }
                document.getElementById('connectBtn').classList.add('active');
            } else {
                document.getElementById('connectBtn').classList.remove('active');
            }
        }

        // Add click handler for connect button
        document.getElementById('connectBtn').addEventListener('click', toggleConnections);

        // Function to create a label for a marker
        function createMarkerLabel(marker, name) {
            const labelIcon = L.divIcon({
                className: 'marker-label',
                html: name,
                iconSize: null
            });
            
            const label = L.marker(marker.getLatLng(), {
                icon: labelIcon,
                zIndexOffset: 1000
            });
            
            // Make label clickable
            label.on('click', () => {
                marker.openPopup();
            });
            
            return label;
        }

        // Add pins to map
        const pins = ''' + json.dumps(pins) + ''';
        
        pins.forEach(pin => {
            const marker = L.marker([pin.lat, pin.lng], {
                icon: redIcon,
                riseOnHover: true
            });
            
            const popupContent = `
                <div>
                    <strong>${pin.name}</strong>
                    ${pin.image ? `<br><img src="${pin.image}" class="popup-image" alt="${pin.name}" onload="this.parentElement.parentElement._leaflet_popup._updateLayout();">` : ''}
                </div>
            `;
            
            marker.bindPopup(popupContent, {
                minWidth: 220,
                maxWidth: 300,
                autoPanPadding: [50, 50]
            });
            
            // Create and add label
            const label = createMarkerLabel(marker, pin.name);
            label.addTo(map);
            
            marker.addTo(map);
            markers.push(marker);
        });
    </script>
</body>
</html>'''

    # Send as downloadable file
    response = make_response(html_template)
    response.headers['Content-Type'] = 'text/html'
    response.headers['Content-Disposition'] = 'attachment; filename=tui-map-snapshot.html'
    return response

if __name__ == '__main__':
    app.run(debug=False)

    # app.run(debug=True, port=5005, threaded=True)
