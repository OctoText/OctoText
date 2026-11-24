import base64
import datetime
import email
import html
import http.server
import io
import json
import os
import socket
import socketserver
import ssl
import threading
from email import policy

from PIL import Image, ImageDraw

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CERT_FILE = os.path.join(BASE_DIR, "smtp.crt")
KEY_FILE = os.path.join(BASE_DIR, "smtp.key")

MESSAGES = []
MESSAGES_LOCK = threading.Lock()


def make_mock_webcam_jpeg():
    img = Image.new("RGB", (480, 280), (30, 41, 59))
    draw = ImageDraw.Draw(img)
    # Colored corners so flip/rotate is unmistakably obvious
    draw.rectangle([0, 0, 90, 60], fill=(220, 38, 38))       # Top-Left: RED
    draw.rectangle([390, 0, 480, 60], fill=(22, 163, 74))   # Top-Right: GREEN
    draw.rectangle([0, 220, 90, 280], fill=(37, 99, 235))   # Bottom-Left: BLUE
    draw.rectangle([390, 220, 480, 280], fill=(234, 179, 8)) # Bottom-Right: YELLOW

    draw.text((15, 22), "TL RED", fill=(255, 255, 255))
    draw.text((400, 22), "TR GRN", fill=(255, 255, 255))
    draw.text((15, 242), "BL BLU", fill=(255, 255, 255))
    draw.text((400, 242), "BR YEL", fill=(0, 0, 0))

    draw.text((215, 18), "TOP ^", fill=(255, 255, 255))
    draw.text((205, 248), "BOTTOM v", fill=(255, 255, 255))
    draw.text((15, 130), "< LEFT", fill=(255, 255, 255))
    draw.text((405, 130), "RIGHT >", fill=(255, 255, 255))

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    draw.text((140, 115), "OctoText Mock Webcam", fill=(248, 250, 252))
    draw.text((165, 145), ts, fill=(148, 163, 184))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def handle_smtp_client(conn, addr, ssl_ctx):
    def send_line(s, text):
        s.sendall((text + "\r\n").encode("utf-8"))

    def read_line(rfile):
        line = rfile.readline()
        if not line:
            return None
        return line.decode("utf-8", errors="replace").rstrip("\r\n")

    s = conn
    rfile = s.makefile("rb")
    try:
        send_line(s, "220 127.0.0.1 OctoText-Mock-SMTP ESMTP ready")
        mail_from = ""
        rcpt_to = []
        while True:
            line = read_line(rfile)
            if line is None:
                break
            upper = line.upper()
            if upper.startswith("EHLO") or upper.startswith("HELO"):
                send_line(s, "250-127.0.0.1")
                send_line(s, "250-STARTTLS")
                send_line(s, "250 AUTH PLAIN LOGIN")
            elif upper == "STARTTLS":
                send_line(s, "220 Ready to start TLS")
                rfile.close()
                s = ssl_ctx.wrap_socket(s, server_side=True)
                rfile = s.makefile("rb")
            elif upper.startswith("AUTH PLAIN"):
                parts = line.split(" ", 2)
                if len(parts) < 3:
                    send_line(s, "334 ")
                    read_line(rfile)
                send_line(s, "235 2.7.0 Authentication successful")
            elif upper.startswith("AUTH LOGIN"):
                send_line(s, "334 VXNlcm5hbWU6")
                read_line(rfile)
                send_line(s, "334 UGFzc3dvcmQ6")
                read_line(rfile)
                send_line(s, "235 2.7.0 Authentication successful")
            elif upper.startswith("MAIL FROM:"):
                mail_from = line[10:].strip()
                send_line(s, "250 OK")
            elif upper.startswith("RCPT TO:"):
                rcpt_to.append(line[8:].strip())
                send_line(s, "250 OK")
            elif upper == "DATA":
                send_line(s, "354 End data with <CR><LF>.<CR><LF>")
                data_lines = []
                while True:
                    dline = rfile.readline()
                    if not dline:
                        break
                    if dline in (b".\r\n", b".\n"):
                        break
                    if dline.startswith(b".."):
                        dline = dline[1:]
                    data_lines.append(dline)
                raw = b"".join(data_lines)
                msg = email.message_from_bytes(raw, policy=policy.default)
                body_text = ""
                attachments = []
                if msg.is_multipart():
                    for part in msg.walk():
                        ctype = part.get_content_type()
                        cdisp = str(part.get("Content-Disposition", ""))
                        if ctype == "text/plain" and "attachment" not in cdisp:
                            body_text += part.get_content()
                        elif ctype.startswith("image/") or "attachment" in cdisp:
                            payload = part.get_payload(decode=True) or b""
                            attachments.append({
                                "filename": part.get_filename() or "attachment.jpg",
                                "content_type": ctype,
                                "size": len(payload),
                                "b64": base64.b64encode(payload).decode("ascii"),
                            })
                else:
                    body_text = msg.get_content()
                record = {
                    "received_at": datetime.datetime.now().strftime("%H:%M:%S"),
                    "from": str(msg.get("From", mail_from)),
                    "to": str(msg.get("To", ", ".join(rcpt_to))),
                    "cc": str(msg.get("Cc", "")),
                    "subject": str(msg.get("Subject", "")),
                    "body": body_text,
                    "attachments": attachments,
                }
                with MESSAGES_LOCK:
                    MESSAGES.insert(0, record)
                send_line(s, "250 OK queued")
            elif upper == "RSET":
                mail_from = ""
                rcpt_to = []
                send_line(s, "250 OK")
            elif upper == "NOOP":
                send_line(s, "250 OK")
            elif upper == "QUIT":
                send_line(s, "221 Bye")
                break
            else:
                send_line(s, "250 OK")
    except Exception as exc:
        print(f"[SMTP] Error from {addr}: {exc}", flush=True)
    finally:
        try:
            rfile.close()
        except Exception:
            pass
        try:
            s.close()
        except Exception:
            pass


def run_smtp_server(host="127.0.0.1", port=925):
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(certfile=CERT_FILE, keyfile=KEY_FILE)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(16)
    print(f"[SMTP] Listening with STARTTLS on {host}:{port}", flush=True)
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=handle_smtp_client, args=(conn, addr, ssl_ctx), daemon=True).start()


class MockHTTPHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/snapshot-fail"):
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"Simulated webcam failure")
            return
        if self.path.startswith("/snapshot"):
            data = make_mock_webcam_jpeg()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path == "/api/messages":
            with MESSAGES_LOCK:
                payload = json.dumps(MESSAGES).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path == "/clear":
            with MESSAGES_LOCK:
                MESSAGES.clear()
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return

        with MESSAGES_LOCK:
            msgs = list(MESSAGES)
        cards = []
        for i, m in enumerate(msgs):
            imgs = ""
            for att in m["attachments"]:
                imgs += (
                    f'<div style="margin-top:8px;"><b>Attachment:</b> {html.escape(att["filename"])} '
                    f'({att["size"]} bytes)<br>'
                    f'<img src="data:{html.escape(att["content_type"])};base64,{att["b64"]}" '
                    f'style="max-width:480px;border:1px solid #cbd5e1;margin-top:4px;"></div>'
                )
            cards.append(
                f'<div style="border:1px solid #cbd5e1;border-radius:8px;padding:12px;margin:12px 0;background:#fff;">'
                f'<div><b>#{len(msgs)-i}</b> [{html.escape(m["received_at"])}] '
                f'<b>Subject:</b> {html.escape(m["subject"])}</div>'
                f'<div><b>From:</b> {html.escape(m["from"])} &rarr; <b>To:</b> {html.escape(m["to"])}</div>'
                f'<pre style="background:#f8fafc;padding:8px;border-radius:4px;">{html.escape(m["body"])}</pre>'
                f'{imgs}</div>'
            )
        if not cards:
            cards.append('<p style="color:#64748b;">No emails received yet. Click "Send a test message" in OctoPrint!</p>')

        page = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>OctoText Local Test Inbox</title>
<meta http-equiv="refresh" content="5">
</head><body style="font-family:system-ui,sans-serif;max-width:820px;margin:24px auto;background:#f1f5f9;padding:0 16px;">
<h2>OctoText Local Test Inbox &amp; Mock Webcam</h2>
<p>
  <b>SMTP Sink (STARTTLS):</b> <code>127.0.0.1:925</code> &nbsp;|&nbsp;
  <b>Mock Webcam:</b> <a href="/snapshot">/snapshot</a> &nbsp;|&nbsp;
  <b>Fail Webcam:</b> <a href="/snapshot-fail">/snapshot-fail</a> &nbsp;|&nbsp;
  <a href="/clear">Clear Inbox</a>
</p>
<p style="font-size:13px;color:#475569;">Auto-refreshes every 5 seconds.</p>
{''.join(cards)}
</body></html>""".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, *args):
        pass


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    threading.Thread(target=run_smtp_server, daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", 8089), MockHTTPHandler)
    print("[HTTP] Inbox & Mock Webcam listening on http://127.0.0.1:8089", flush=True)
    httpd.serve_forever()
