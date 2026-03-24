"""
本地价格监控 Web 服务器
运行: python server.py
访问: http://localhost:8899
"""
import csv
import json
import os
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR  = Path(__file__).parent
CSV_PATH  = BASE_DIR / "competitor_prices.csv"
HTML_PATH = BASE_DIR / "index.html"
PORT      = 8899


def read_csv_as_json() -> dict:
    if not CSV_PATH.exists():
        return {"headers": [], "rows": [], "dates": [], "updated": ""}

    with open(CSV_PATH, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        rows = list(reader)

    date_cols = sorted([h for h in headers if h.startswith("Price_")])
    base_cols = ["产品线", "竞争品牌", "竞品型号", "ASIN", "US ASIN Link"]

    result_rows = []
    for row in rows:
        r = {c: row.get(c, "") for c in base_cols}
        for d in date_cols:
            r[d] = row.get(d, "")
        result_rows.append(r)

    mtime = CSV_PATH.stat().st_mtime
    import datetime
    updated = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")

    return {
        "headers": base_cols + date_cols,
        "dates":   date_cols,
        "rows":    result_rows,
        "updated": updated,
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/data":
            data = read_csv_as_json()
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return

        # 其他路径交给默认静态文件处理
        super().do_GET()

    def log_message(self, fmt, *args):
        print(f"  {args[0]} {args[1]}")


if __name__ == "__main__":
    print(f"=== Price Monitor Server ===")
    print(f"URL : http://localhost:{PORT}")
    print(f"Data: {CSV_PATH}")
    print(f"Press Ctrl+C to stop\n")
    server = HTTPServer(("localhost", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
