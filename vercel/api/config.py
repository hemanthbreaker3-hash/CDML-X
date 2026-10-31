"""Expose only public website settings to the static frontend."""
import json
import os
from http.server import BaseHTTPRequestHandler

try:
    import config as local_config
except Exception:
    local_config = None

def setting(name, default):
    value = os.getenv(name)
    if value:
        return value
    return getattr(local_config, name, default) if local_config else default

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = {
            "site_name": setting("SITE_NAME", "CANTARELLABOTS"),
            "site_tagline": setting("SITE_TAGLINE", "Your media hub, beautifully organized."),
        }
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
