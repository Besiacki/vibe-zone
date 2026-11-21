"""SQLite-backed mini social API.

Writes commit to a WAL database before the HTTP response is sent, with
synchronous=NORMAL. That setting is durable across SIGKILL (application crash):
SQLite still recovers the WAL. It is not a power-loss mode. Feed reads are served
from the newest 20 posts cached in memory and rebuilt from SQLite at startup.

The socket loop is a single-threaded epoll/kqueue server. One vCPU cannot afford
an asyncio callback and a thread hop on every connection, and 5000 idle
keep-alive clients do not fit in the 4096 file-descriptor cap.
"""
import errno
import gc
import json
import os
import re
import selectors
import signal
import socket
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

MAX_BODY = 16 * 1024
MAX_DRAIN = 256 * 1024
NAME = re.compile(r"[a-zA-Z0-9_]{1,32}\Z")
REASONS = {200: "OK", 201: "Created", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error"}
POST_NOT_FOUND = json.dumps({"error": "Post not found"}, separators=(",", ":")).encode()
FEED_PREFIX = b"GET /feed HTTP/1.1\r\n"
HEALTH_PREFIX = b"GET /health HTTP/1.1\r\n"
READ = selectors.EVENT_READ
WRITE = selectors.EVENT_WRITE

feed = []
like_counts = {}
FEED_CLOSE = b""
db = None
WRITES_SINCE_CHECKPOINT = 0
STOP = False


def utc_now():
    moment = datetime.now(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def error_json(message):
    return json.dumps({"error": message}, separators=(",", ":")).encode()


def http_response(status, body):
    head = (
        f"HTTP/1.1 {status} {REASONS[status]}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode()
    return head + body


def dump_post(post):
    return json.dumps(
        {
            "id": post["id"],
            "author": post["author"],
            "content": post["content"],
            "created_at": post["created_at"],
            "likes": post["likes"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def rebuild_feed():
    global FEED_CLOSE
    body = b'{"posts":[]}' if not feed else b'{"posts":[' + b",".join(post["_json"] for post in feed) + b"]}"
    FEED_CLOSE = http_response(200, body)


BAD_REQUEST = http_response(400, error_json("Bad request"))
BODY_LIMIT = http_response(400, error_json("Request body exceeds 16 KiB"))
SERVER_ERROR = http_response(500, error_json("Internal error"))
HEALTH_CLOSE = http_response(200, b'{"status":"ok"}')


def valid_name(value):
    return isinstance(value, str) and NAME.fullmatch(value) is not None


def validate_post(payload):
    author, content = payload.get("author"), payload.get("content")
    if not valid_name(author):
        raise ValueError("author must match [a-zA-Z0-9_]{1,32}")
    if not isinstance(content, str) or not 1 <= len(content) <= 280 or not content.strip():
        raise ValueError("content must contain 1..280 characters and not be blank")
    try:
        content.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("content must be valid UTF-8") from None
    return author, content


def validate_like(payload):
    post_id, user = payload.get("post_id"), payload.get("user")
    if type(post_id) is not int or post_id < 1 or post_id > 9223372036854775807 or not valid_name(user):
        raise ValueError("Expected a positive integer post_id and a valid user")
    return post_id, user


def parse_object(raw, content_type):
    media = (content_type or "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        raise ValueError("Content-Type must be application/json")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("Expected a JSON object") from None
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def init_db(path):
    global db
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, isolation_level=None, timeout=10, check_same_thread=True)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA temp_store=MEMORY")
    db.execute("PRAGMA cache_size=-32768")
    db.execute("PRAGMA mmap_size=0")
    db.execute("PRAGMA wal_autocheckpoint=0")
    db.execute("PRAGMA journal_size_limit=67108864")
    db.execute("PRAGMA busy_timeout=5000")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS likes (
            post_id INTEGER NOT NULL,
            user TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS likes_post_user ON likes(post_id, user);
    """)
    like_counts.clear()
    feed.clear()
    for post_id, count in db.execute("SELECT post_id, COUNT(*) FROM likes GROUP BY post_id"):
        like_counts[post_id] = count
    for row in db.execute("SELECT id, author, content, created_at FROM posts ORDER BY id DESC LIMIT 20"):
        post = {
            "id": row[0],
            "author": row[1],
            "content": row[2],
            "created_at": row[3],
            "likes": like_counts.get(row[0], 0),
        }
        post["_json"] = dump_post(post)
        feed.append(post)
    rebuild_feed()
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.execute("PRAGMA locking_mode=EXCLUSIVE")
    db.execute("BEGIN IMMEDIATE")
    db.execute("COMMIT")


def safe_rollback():
    try:
        db.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def apply_like(post_id, user, pending):
    if db.execute("SELECT 1 FROM posts WHERE id = ?", (post_id,)).fetchone() is None:
        return ("missing",)
    before = db.total_changes
    db.execute("INSERT OR IGNORE INTO likes(post_id, user) VALUES (?, ?)", (post_id, user))
    inserted = db.total_changes != before
    if post_id in pending:
        count = pending[post_id] + inserted
        pending[post_id] = count
        return ("like", post_id, count)
    if post_id in like_counts:
        count = like_counts[post_id] + inserted
        if inserted:
            pending[post_id] = count
        return ("like", post_id, count)
    count = db.execute("SELECT COUNT(*) FROM likes WHERE post_id = ?", (post_id,)).fetchone()[0]
    pending[post_id] = count
    return ("like", post_id, count)


def apply_sql(job, pending):
    kind = job[0]
    if kind == "post":
        author, content = job[1], job[2]
        created = utc_now()
        cursor = db.execute(
            "INSERT INTO posts(author, content, created_at) VALUES (?, ?, ?)",
            (author, content, created),
        )
        post = {"id": cursor.lastrowid, "author": author, "content": content, "created_at": created, "likes": 0}
        pending[post["id"]] = 0
        return ("post", post)
    return apply_like(job[1], job[2], pending)


def remember(outcomes, pending):
    like_counts.update(pending)
    changed = False
    results = []
    for item in outcomes:
        if item[0] == "post":
            post = item[1]
            post["_json"] = dump_post(post)
            results.append((201, post["_json"]))
            feed.insert(0, post)
            changed = True
        elif item[0] == "like":
            body = json.dumps(
                {"post_id": item[1], "likes": item[2], "liked": True},
                separators=(",", ":"),
            ).encode()
            results.append((200, body))
            for post in feed:
                if post["id"] == item[1] and post["likes"] != item[2]:
                    post["likes"] = item[2]
                    post["_json"] = dump_post(post)
                    changed = True
                    break
        else:
            results.append((404, POST_NOT_FOUND))
    if len(feed) > 20:
        del feed[20:]
        changed = True
    if changed:
        rebuild_feed()
    return results


def perform(job):
    global WRITES_SINCE_CHECKPOINT
    pending = {}
    if db.in_transaction:
        safe_rollback()
    db.execute("BEGIN IMMEDIATE")
    try:
        outcome = apply_sql(job, pending)
        db.execute("COMMIT")
    except Exception:
        safe_rollback()
        raise
    WRITES_SINCE_CHECKPOINT += 1
    if WRITES_SINCE_CHECKPOINT >= 100000:
        WRITES_SINCE_CHECKPOINT = 0
        db.execute("PRAGMA wal_checkpoint(PASSIVE)")
    return remember([outcome], pending)[0]


def headers_of(lines):
    headers = {}
    for line in lines:
        if b":" not in line:
            raise ValueError("Malformed header")
        name, value = line.split(b":", 1)
        key = name.decode("latin1").strip().lower()
        if not key:
            raise ValueError("Malformed header")
        if key in headers and key in ("content-length", "content-type", "transfer-encoding"):
            raise ValueError("Duplicate header")
        headers[key] = value.decode("latin1").strip()
    return headers


def dispatch(method, path, headers, body):
    if method == "GET" and path == "/health":
        return HEALTH_CLOSE
    if method == "GET" and path == "/feed":
        return FEED_CLOSE
    if method == "POST" and path in ("/posts", "/like"):
        try:
            payload = parse_object(body, headers.get("content-type"))
            if path == "/posts":
                author, content = validate_post(payload)
                job = ("post", author, content)
            else:
                post_id, user = validate_like(payload)
                job = ("like", post_id, user)
        except ValueError as exc:
            return http_response(400, error_json(str(exc)))
        try:
            status, response_body = perform(job)
        except Exception:
            return SERVER_ERROR
        return http_response(status, response_body)
    return http_response(404, error_json("Not found"))


class Conn:
    __slots__ = ("sock", "buf", "out", "sent", "seen", "watched", "header_end", "meta", "discard")

    def __init__(self, sock):
        self.sock = sock
        self.buf = None
        self.out = None
        self.sent = 0
        self.seen = time.monotonic()
        self.watched = False
        self.header_end = None
        self.meta = None
        self.discard = 0


def serve(port):
    global STOP
    selector = selectors.DefaultSelector()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", port))
    listener.listen(4096)
    listener.setblocking(False)
    selector.register(listener, READ, None)
    waiting = []
    accept_paused = False
    quickack = getattr(socket, "TCP_QUICKACK", None)

    def drop(conn):
        nonlocal accept_paused
        if conn.watched:
            try:
                selector.unregister(conn.sock)
            except (KeyError, ValueError):
                pass
            conn.watched = False
        try:
            conn.sock.close()
        except OSError:
            pass
        if accept_paused:
            selector.register(listener, READ, None)
            accept_paused = False

    def watch(conn, event):
        conn.seen = time.monotonic()
        if conn.watched:
            selector.modify(conn.sock, event, conn)
        else:
            selector.register(conn.sock, event, conn)
            conn.watched = True
            waiting.append(conn)

    def try_send(conn):
        payload = conn.out
        try:
            chunk = payload if conn.sent == 0 else memoryview(payload)[conn.sent:]
            sent = conn.sock.send(chunk)
        except BlockingIOError:
            watch(conn, WRITE)
            return
        except OSError:
            drop(conn)
            return
        if sent <= 0:
            drop(conn)
            return
        conn.sent += sent
        if conn.sent >= len(payload):
            drop(conn)
            return
        watch(conn, WRITE)

    def arm(conn, payload):
        conn.buf = None
        conn.out = payload
        conn.sent = 0
        try_send(conn)

    def stash(conn, blob):
        if conn.buf is None:
            conn.buf = bytearray(blob)

    def respond_parsed(conn, method, path, headers, body):
        try:
            arm(conn, dispatch(method, path, headers, body))
        except Exception as exc:
            print(f"request error: {exc.__class__.__name__}: {exc}", file=sys.stderr, flush=True)
            arm(conn, SERVER_ERROR)

    def pull(conn):
        try:
            chunk = conn.sock.recv(8192)
        except BlockingIOError:
            watch(conn, READ)
            return
        except OSError:
            drop(conn)
            return
        if not chunk:
            drop(conn)
            return
        conn.seen = time.monotonic()
        if conn.discard:
            conn.discard -= len(chunk)
            if conn.discard <= 0:
                arm(conn, BODY_LIMIT)
            else:
                watch(conn, READ)
            return
        if conn.buf is not None:
            conn.buf.extend(chunk)
            blob = conn.buf
        else:
            blob = chunk
        if conn.header_end is None:
            header_end = blob.find(b"\r\n\r\n")
            if header_end < 0:
                if len(blob) > 8192:
                    arm(conn, BAD_REQUEST)
                    return
                stash(conn, blob)
                watch(conn, READ)
                return
            if blob.startswith(FEED_PREFIX):
                arm(conn, FEED_CLOSE)
                return
            if blob.startswith(HEALTH_PREFIX):
                arm(conn, HEALTH_CLOSE)
                return
            head = blob[:header_end]
            lines = head.split(b"\r\n")
            if not lines or len(lines) > 64 or len(lines[0]) > 2048:
                arm(conn, BAD_REQUEST)
                return
            parts = lines[0].decode("latin1").split(" ")
            if len(parts) != 3 or not parts[1].startswith("/"):
                arm(conn, BAD_REQUEST)
                return
            method, path = parts[0], parts[1].split("?", 1)[0]
            try:
                headers = headers_of(lines[1:])
            except ValueError as exc:
                arm(conn, http_response(400, error_json(str(exc))))
                return
            if "transfer-encoding" in headers:
                arm(conn, http_response(400, error_json("Chunked transfer is not supported")))
                return
            raw_length = headers.get("content-length")
            if raw_length is None:
                length = 0
            elif not raw_length.isdigit():
                arm(conn, http_response(400, error_json("Invalid Content-Length")))
                return
            else:
                length = int(raw_length)
            if length > MAX_BODY:
                have = len(blob) - (header_end + 4)
                target = min(length, MAX_DRAIN)
                if have >= target:
                    arm(conn, BODY_LIMIT)
                    return
                conn.discard = target - have
                stash(conn, blob)
                watch(conn, READ)
                return
            body_at = header_end + 4
            have = len(blob) - body_at
            if have >= length:
                body = bytes(blob[body_at:body_at + length])
                respond_parsed(conn, method, path, headers, body)
                return
            conn.header_end = header_end
            conn.meta = (method, path, headers, length)
            stash(conn, blob)
            watch(conn, READ)
            return
        method, path, headers, length = conn.meta
        body_at = conn.header_end + 4
        if len(blob) - body_at < length:
            watch(conn, READ)
            return
        body = bytes(blob[body_at:body_at + length])
        respond_parsed(conn, method, path, headers, body)

    def accept_all():
        nonlocal accept_paused
        while True:
            try:
                sock, _addr = listener.accept()
            except BlockingIOError:
                return
            except OSError as exc:
                if exc.errno in (errno.EMFILE, errno.ENFILE) and not accept_paused:
                    selector.unregister(listener)
                    accept_paused = True
                    print("accept paused: too many open files", file=sys.stderr, flush=True)
                return
            sock.setblocking(False)
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                if quickack is not None:
                    sock.setsockopt(socket.IPPROTO_TCP, quickack, 1)
            except OSError:
                pass
            pull(Conn(sock))

    def sweep(now):
        if not waiting:
            return
        alive = []
        for conn in waiting:
            if not conn.watched:
                continue
            if now - conn.seen > 8:
                drop(conn)
            else:
                alive.append(conn)
        waiting[:] = alive
        gc.collect()

    def request_stop(*_args):
        global STOP
        STOP = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    gc.disable()
    gc.collect()
    gc.freeze()
    print(f"listening ('0.0.0.0', {port})", flush=True)
    next_sweep = time.monotonic() + 1
    while not STOP:
        events = selector.select(0.5)
        if events:
            for key, mask in events:
                if key.fileobj is listener:
                    accept_all()
                elif mask & WRITE:
                    try_send(key.data)
                else:
                    pull(key.data)
        now = time.monotonic()
        if now >= next_sweep:
            sweep(now)
            next_sweep = now + 1
    try:
        if db.in_transaction:
            safe_rollback()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.close()
    except sqlite3.Error as exc:
        print(f"shutdown error: {exc}", file=sys.stderr, flush=True)
    selector.close()
    listener.close()


def main():
    sys.dont_write_bytecode = True
    port = int(os.environ.get("PORT", "8080"))
    data_dir = os.environ.get("DATA_DIR", "/data")
    try:
        init_db(Path(data_dir) / "social.sqlite3")
    except Exception as exc:
        print(f"startup failed: {exc.__class__.__name__}: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None
    try:
        serve(port)
    except KeyboardInterrupt:
        raise SystemExit(0) from None


if __name__ == "__main__":
    main()
