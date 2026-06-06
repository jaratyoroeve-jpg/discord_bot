from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        print(body)
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length else ""
        print(f"GET {self.path}" + (f" body={body}" if body else ""))
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass

if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", 8082), Handler)
    print("Listening on http://127.0.0.1:8082")
    server.serve_forever()
