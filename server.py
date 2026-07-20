#!/usr/bin/env python3
"""LibreOffice conversion microservice. Stdlib only.

POST /convert   (header X-Auth-Token: <SHARED_TOKEN>)
    multipart form: file=<upload>, to=<target ext>
    -> {"id": "...", "filename": "name.ppt"}   runs soffice, stores /tmp/out/<id>/<name>.<to>

GET /out/<id>/<name>
    -> streams the converted file once (unguessable id; no auth so the edge proxy can fetch it)

GET /health -> "ok"
"""
import json
import os
import re
import secrets
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

TOKEN = os.environ.get("SHARED_TOKEN", "")
OUT_ROOT = "/tmp/out"
# Engine dispatch by (from_ext, to). Each (from,to) either maps to a valid plan or is rejected —
# the plan itself is the allowlist, so the endpoint is never a general exec surface.
# ponytail: to add a conversion, extend one of these sets; unlisted pairs 400 automatically.

# Office <-> office via LibreOffice. Legacy binary targets need their export filter named
# explicitly — bare `--convert-to ppt` gives "no export filter found".
OFFICE_IN = {"doc", "docx", "odt", "rtf", "ppt", "pptx", "odp", "pps", "ppsx", "potx", "xls", "xlsx", "ods"}
LO_OUT = {"ppt", "pptx", "doc", "docx", "odp", "odt", "xls", "xlsx", "ods", "rtf"}
FILTERS = {"ppt": "MS PowerPoint 97", "doc": "MS Word 97", "xls": "MS Excel 97"}

# Vector/legacy drawing -> svg, also via LibreOffice Draw.
SVG_IN = {"wmf", "emf", "cdr"}

# Video containers -> modern containers via ffmpeg. ffmpeg reports per-file failure for codecs
# it can't decode (some rmvb/swf/wtv), which surfaces as a normal conversion error.
VIDEO_IN = {"ts", "vob", "mpeg", "mpg", "rmvb", "m2ts", "mxf", "swf", "wtv", "3gp", "flv", "ogv", "mp4", "webm", "mkv", "mov", "avi"}
VIDEO_OUT = {"mp4", "webm", "mkv", "mov", "avi"}

# RAW photo -> jpg/png: dcraw decodes to TIFF, ImageMagick re-encodes.
RAW_IN = {"nef", "cr2", "cr3", "arw", "dng", "crw", "raf", "rw2", "orf", "pef", "srw"}
RAW_OUT = {"jpg", "png"}

SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def build_plan(from_ext, to, in_path, work, stem, profile):
    """Return (list_of_argv_steps, final_output_path) or (None, None) if the pair is unsupported.
    Steps run in sequence; the last step must produce final_output_path."""
    out = os.path.join(work, "%s.%s" % (stem, to))
    soffice = ["soffice", "--headless", "--norestore", "-env:UserInstallation=%s" % profile]

    if from_ext in OFFICE_IN and to in LO_OUT and from_ext != to:
        arg = "%s:%s" % (to, FILTERS[to]) if to in FILTERS else to
        return [soffice + ["--convert-to", arg, "--outdir", work, in_path]], out

    if from_ext in SVG_IN and to == "svg":
        return [soffice + ["--convert-to", "svg", "--outdir", work, in_path]], out

    if from_ext in VIDEO_IN and to in VIDEO_OUT and from_ext != to:
        return [["ffmpeg", "-y", "-i", in_path, out]], out

    if from_ext in RAW_IN and to in RAW_OUT:
        tiff = os.path.join(work, "%s.tiff" % stem)  # dcraw -T writes <stem>.tiff beside input
        return [["dcraw", "-T", "-w", in_path], ["convert", tiff, out]], out

    return None, None


def sanitize(name, fallback):
    base = os.path.basename(name or "")
    base = SAFE_NAME.sub("_", base).lstrip(".")
    return base or fallback


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health" or self.path == "/":
            self.send_response(200)
            self.send_header("content-length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        m = re.match(r"^/out/([a-z0-9]+)/([^/]+)$", self.path)
        if not m:
            self._json({"error": "not found"}, 404)
            return
        job_id, name = m.group(1), sanitize(unquote(m.group(2)), "download")
        path = os.path.join(OUT_ROOT, job_id, name)
        if not os.path.isfile(path):
            self._json({"error": "expired or not found"}, 404)
            return
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("content-type", "application/octet-stream")
        self.send_header("content-length", str(size))
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def do_POST(self):
        if self.path != "/convert":
            self._json({"error": "not found"}, 404)
            return
        if not TOKEN or self.headers.get("X-Auth-Token") != TOKEN:
            self._json({"error": "unauthorized"}, 401)
            return
        try:
            fields = self._parse_multipart()
        except Exception as e:
            self._json({"error": "bad request: %s" % e}, 400)
            return

        to = str(fields.get("to_value", "")).lower().strip()
        from_ext = str(fields.get("from_value", "")).lower().strip()
        upload = fields.get("file")
        if not upload:
            self._json({"error": "no file"}, 400)
            return

        job_id = secrets.token_hex(16)
        work = os.path.join(OUT_ROOT, job_id)
        os.makedirs(work, exist_ok=True)
        in_name = sanitize(upload["filename"], "input")
        in_path = os.path.join(work, in_name)
        with open(in_path, "wb") as f:
            f.write(upload["data"])

        stem = os.path.splitext(in_name)[0]
        # Fall back to the input file's own extension if the client didn't send `from`.
        if not from_ext:
            from_ext = (os.path.splitext(in_name)[1].lstrip(".") or "").lower()

        # Unique profile dir avoids LibreOffice's single-instance lock across concurrent requests.
        profile = "file://%s/profile" % work
        steps, out_path = build_plan(from_ext, to, in_path, work, stem, profile)
        if steps is None:
            self._json({"error": "unsupported conversion %s -> %s" % (from_ext, to)}, 400)
            return

        try:
            for argv in steps:
                proc = subprocess.run(argv, capture_output=True, timeout=120, cwd=work)
        except subprocess.TimeoutExpired:
            self._json({"error": "conversion timed out"}, 504)
            return
        except FileNotFoundError as e:
            self._json({"error": "converter tool missing: %s" % e}, 500)
            return

        if not os.path.isfile(out_path):
            detail = (proc.stderr or proc.stdout or b"").decode(errors="replace")[:200]
            self._json({"error": "conversion failed: %s" % (detail or "no output produced")}, 502)
            return
        try:
            os.remove(in_path)
        except OSError:
            pass
        self._json({"id": job_id, "filename": os.path.basename(out_path)})

    def _parse_multipart(self):
        ctype = self.headers.get("content-type", "")
        m = re.search(r"boundary=([^;]+)", ctype)
        if not m:
            raise ValueError("not multipart")
        boundary = m.group(1).strip('"').encode()
        length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(length)
        parts = body.split(b"--" + boundary)
        out = {}
        for part in parts:
            part = part.strip(b"\r\n")
            if not part or part == b"--":
                continue
            head, _, data = part.partition(b"\r\n\r\n")
            head_s = head.decode(errors="replace")
            nm = re.search(r'name="([^"]+)"', head_s)
            if not nm:
                continue
            field = nm.group(1)
            fn = re.search(r'filename="([^"]*)"', head_s)
            if fn is not None:
                out[field] = {"filename": fn.group(1), "data": data}
            else:
                out["%s_value" % field] = data.decode(errors="replace").strip()
        return out

    def log_message(self, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), args[0] % args[1:]))


if __name__ == "__main__":
    os.makedirs(OUT_ROOT, exist_ok=True)
    port = int(os.environ.get("PORT", "7860"))  # HF Spaces default app_port
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
