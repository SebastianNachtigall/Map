import sys
path = '/home/SebastianNachtigall/Map'
if path not in sys.path:
    sys.path.append(path)

from app import app as application