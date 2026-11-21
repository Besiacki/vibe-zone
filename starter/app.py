"""SQLite-backed mini social API.

Writes commit to a WAL database before the HTTP response is sent, with
synchronous=NORMAL. That setting is durable across SIGKILL (application crash):
SQLite still recovers the WAL. It is not a power-loss mode. Feed reads are served
from the newest 20 posts cached in memory and rebuilt from SQLite at startup.
"""
import asyncio
import json
import os
import queue
import re
import signal
import socket
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

MAX_BODY = 16 * 1024
NAME = re.compile(r"[a-zA-Z0-9_]{1,32}\Z")
REASONS = {200: "OK", 201: "Created", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error"}
POST_NOT_FOUND = json.dumps({"error": "Post not found"}, separators=(",", ":")).encode()

feed = []
like_counts = {}
FEED_HTTP = b""
db = None
JOBS = queue.SimpleQueue()
WRITES_SINCE_CHECKPOINT = 0


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
    global FEED_HTTP
    body = b'{"posts":[]}' if not feed else b'{"posts":[' + b",".join(post["_json"] for post in feed) + b"]}"
    FEED_HTTP = http_response(200, body)


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
    db = sqlite3.connect(path, isolation_level=None, timeout=10, check_same_thread=False)
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
    # Fold a recovered WAL back into the database so /data stays small after SIGKILL.
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


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


def finish(futures, results):
    for fut, result in zip(futures, results):
        if not fut.done():
            fut.set_result(result)


def fail(fut, exc):
    def reject(bound=fut, error=exc):
        if not bound.done():
            bound.set_exception(error)
    return reject


def process_batch(batch):
    global WRITES_SINCE_CHECKPOINT
    pending = {}
    if db.in_transaction:
        safe_rollback()
    db.execute("BEGIN IMMEDIATE")
    try:
        outcomes = [apply_sql(job, pending) for job, _fut in batch]
        db.execute("COMMIT")
    except Exception as exc:
        safe_rollback()
        if len(batch) == 1:
            loop = batch[0][1].get_loop()
            loop.call_soon_threadsafe(fail(batch[0][1], exc))
            print(f"sqlite error: {exc.__class__.__name__}: {exc}", file=sys.stderr, flush=True)
            return
        for item in batch:
            process_batch([item])
        return
    results = remember(outcomes, pending)
    loop = batch[0][1].get_loop()
    loop.call_soon_threadsafe(finish, [fut for _job, fut in batch], results)
    WRITES_SINCE_CHECKPOINT += len(batch)
    if WRITES_SINCE_CHECKPOINT >= 4000:
        WRITES_SINCE_CHECKPOINT = 0
        try:
            db.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.Error as exc:
            print(f"checkpoint error: {exc}", file=sys.stderr, flush=True)


def worker():
    while True:
        first = JOBS.get()
        if first is None:
            try:
                if db.in_transaction:
                    safe_rollback()
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                db.close()
            except sqlite3.Error as exc:
                print(f"shutdown error: {exc}", file=sys.stderr, flush=True)
            return
        batch = [first]
        while len(batch) < 32:
            try:
                nxt = JOBS.get_nowait()
            except queue.Empty:
                break
            if nxt is None:
                JOBS.put(None)
                break
            batch.append(nxt)
        process_batch(batch)


async def submit(job):
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    JOBS.put((job, fut))
    return await fut


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


async def read_body(reader, headers):
    if "transfer-encoding" in headers:
        raise ValueError("Chunked transfer is not supported")
    raw_length = headers.get("content-length")
    if raw_length is None:
        return b""
    if not raw_length.isdigit():
        raise ValueError("Invalid Content-Length")
    length = int(raw_length)
    if length > MAX_BODY:
        if length <= 256 * 1024:
            remaining = length
            while remaining:
                chunk = await reader.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        raise ValueError("Request body exceeds 16 KiB")
    if length == 0:
        return b""
    return await reader.readexactly(length)


async def dispatch(method, path, headers, body):
    if method == "GET" and path == "/health":
        return http_response(200, b'{"status":"ok"}')
    if method == "GET" and path == "/feed":
        return FEED_HTTP
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
            status, response_body = await submit(job)
        except Exception:
            return http_response(500, error_json("Internal error"))
        return http_response(status, response_body)
    return http_response(404, error_json("Not found"))


async def handle(reader, writer):
    try:
        sock = writer.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
        try:
            blob = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, asyncio.LimitOverrunError):
            return
        head, _, _rest = blob.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        if not lines or len(lines) > 64 or len(lines[0]) > 2048:
            writer.write(http_response(400, error_json("Bad request")))
            await writer.drain()
            return
        parts = lines[0].decode("latin1").split(" ")
        if len(parts) != 3 or not parts[1].startswith("/"):
            writer.write(http_response(400, error_json("Bad request")))
            await writer.drain()
            return
        method, path = parts[0], parts[1].split("?", 1)[0]
        try:
            header_map = headers_of(lines[1:])
            body = await asyncio.wait_for(read_body(reader, header_map), 10)
            response = await dispatch(method, path, header_map, body)
        except ValueError as exc:
            response = http_response(400, error_json(str(exc)))
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            return
        writer.write(response)
        await writer.drain()
    except (ConnectionError, BrokenPipeError, asyncio.IncompleteReadError):
        return
    except Exception as exc:
        print(f"request error: {exc.__class__.__name__}: {exc}", file=sys.stderr, flush=True)
        try:
            writer.write(http_response(500, error_json("Internal error")))
            await writer.drain()
        except Exception:
            return
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def serve(port):
    thread = threading.Thread(target=worker, name="sqlite-writer", daemon=False)
    thread.start()
    server = await asyncio.start_server(handle, "0.0.0.0", port, backlog=4096, limit=65536)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_args: stop.set())
    sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    print(f"listening {sockets}", flush=True)
    async with server:
        await stop.wait()
        server.close()
        await server.wait_closed()
    JOBS.put(None)
    await asyncio.to_thread(thread.join, 4)


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
        asyncio.run(serve(port))
    except KeyboardInterrupt:
        raise SystemExit(0) from None


if __name__ == "__main__":
    main()
