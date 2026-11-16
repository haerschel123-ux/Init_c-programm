#!/usr/bin/env python3
"""Local operator console and a separate authenticated DayZ mission endpoint."""

import argparse
import hmac
import json
import math
import os
import re
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
PROTOCOL = 1
ONLINE_SECONDS = 20
QUEUE_SECONDS = 20
RECEIPT_SECONDS = 30
MAX_BODY_BYTES = 131_072
MAX_PENDING = 8
MAX_HISTORY = 60
MAX_VEHICLES = 24
IDENTIFIER = re.compile(r"[A-Za-z0-9_+=./-]{1,128}", re.ASCII)
SESSION_ID = re.compile(r"dayz-[0-9]{9}-[0-9]{9}", re.ASCII)
COMMAND_ID = re.compile(r"[0-9a-f]{24}", re.ASCII)
VEHICLE_ID = re.compile(r"car-[1-9][0-9]{0,8}", re.ASCII)
CLASS_NAME = re.compile(r"[A-Za-z0-9_]{1,80}", re.ASCII)
BRIDGE_KEY = re.compile(r"[A-Za-z0-9_-]{43}", re.ASCII)
HOST_NAME = re.compile(r"[A-Za-z0-9.-]{1,253}", re.ASCII)
ITEMS = {
    "BandageDressing": "Verband",
    "BakedBeansCan": "Bohnendose",
    "SodaCan_Pipsi": "Pipsi",
    "WaterBottle": "Wasserflasche",
    "M4A1": "M4-A1",
    "AKM": "KA-M",
    "Mag_STANAG_30Rnd": "STANAG-Magazin · 30 Schuss",
    "Mag_AKM_30Rnd": "KA-M-Magazin · 30 Schuss",
}
CARS = {
    "OffroadHatchback": "Ada 4x4",
    "CivilianSedan": "Olga 24",
    "Hatchback_02": "Gunter 2",
    "Sedan_02": "Sarka 120",
}
ACTIONS = {
    "ping": "Verbindung testen",
    "notify": "Spieler benachrichtigen",
    "teleport": "Spieler teleportieren",
    "give": "Item ins Inventar legen",
    "spawn_car": "Fahrzeug spawnen",
    "remove_car": "Testfahrzeug entfernen",
}
EMPTY_REPLY = {
    "protocol": PROTOCOL,
    "id": "",
    "action": "",
    "target": "",
    "typeName": "",
    "quantity": 0,
    "x": 0.0,
    "z": 0.0,
    "heading": 0.0,
    "message": "",
}


class RequestError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def invalid(message):
    return RequestError(HTTPStatus.BAD_REQUEST, message)


def validated_text(value, description, maximum, pattern=None):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(ch) < 32 or 0xD800 <= ord(ch) <= 0xDFFF for ch in value)
        or (pattern and not pattern.fullmatch(value))
    ):
        raise invalid(f"{description} ist ungültig.")
    return value


def validated_number(value, description, low, high):
    if type(value) not in (int, float) or not low <= value <= high or not math.isfinite(value):
        raise invalid(f"{description} liegt außerhalb des gültigen Bereichs.")
    return float(value)


def bridge_origin(value, local_bridge):
    value = validated_text(value, "Bridge-Adresse", 300).strip()
    if value.rstrip("/") == local_bridge:
        return local_bridge
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise invalid("Die Bridge-Adresse ist ungültig.") from exc
    if (
        parts.scheme != "https"
        or not parts.hostname
        or not HOST_NAME.fullmatch(parts.hostname)
        or parts.username is not None
        or parts.password is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or port == 0
    ):
        raise invalid("Gib eine HTTPS-Basisadresse ohne Pfad ein oder nutze die lokale Vorgabe.")
    return f"https://{parts.netloc.lower()}"


def read_bridge_key(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_path = directory / "bridge.key"
    try:
        descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        descriptor = None
    if descriptor is not None:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(secrets.token_urlsafe(32) + "\n")
    key = key_path.read_text(encoding="ascii").strip()
    if not BRIDGE_KEY.fullmatch(key):
        raise ValueError("Ungültiger Verbindungsschlüssel. Benenne bridge.key um und starte neu.")
    if os.name == "posix":
        key_path.chmod(0o600)
    return key


def equal_secret(supplied, expected):
    return (
        isinstance(supplied, str)
        and supplied.isascii()
        and hmac.compare_digest(supplied, expected)
    )


def collect_entities(rows, world_size, *, vehicles=False):
    limit = MAX_VEHICLES if vehicles else 256
    if not isinstance(rows, list) or len(rows) > limit:
        raise invalid("Die übermittelte Entitätenliste ist ungültig.")
    entities = {}
    for row in rows:
        if not isinstance(row, dict):
            raise invalid("Ein Eintrag in der Entitätenliste ist ungültig.")
        pattern = VEHICLE_ID if vehicles else IDENTIFIER
        entity_id = validated_text(row.get("id"), "Entitäten-ID", 128, pattern)
        if entity_id in entities:
            raise invalid("Doppelte Entitäten-ID.")
        label_key = "typeName" if vehicles else "name"
        label_pattern = CLASS_NAME if vehicles else None
        entry = {
            "id": entity_id,
            label_key: validated_text(row.get(label_key), "Bezeichnung", 100, label_pattern),
            "x": validated_number(row.get("x"), "Entitäten-X", -world_size, 2 * world_size),
            "y": validated_number(row.get("y"), "Entitäten-Y", -5000, 5000),
            "z": validated_number(row.get("z"), "Entitäten-Z", -world_size, 2 * world_size),
        }
        for flag in (("occupied",) if vehicles else ("alive", "inVehicle")):
            if type(row.get(flag)) is not bool:
                raise invalid("Ein Entitätenstatus ist ungültig.")
            entry[flag] = row[flag]
        entities[entity_id] = entry
    return entities


@dataclass
class Command:
    action: str
    label: str
    peer: str
    arguments: dict
    created: float
    created_mono: float
    id: str = field(default_factory=lambda: secrets.token_hex(12))
    status: str = "queued"
    detail: str = ""
    sent_mono: float = 0

    def wire(self):
        return {
            **EMPTY_REPLY,
            "id": self.id,
            "action": self.action,
            **self.arguments,
        }

    def display(self):
        return {
            "id": self.id,
            "label": self.label,
            "status": self.status,
            "detail": self.detail,
            "created": self.created,
        }


class PanelState:
    def __init__(self, token, bridge_port, markup):
        self.token = token
        self.csrf = secrets.token_urlsafe(24)
        self.nonce = secrets.token_urlsafe(18)
        self.bridge_url = f"http://127.0.0.1:{bridge_port}"
        self.page = (
            markup.replace("__PANEL_NONCE__", self.nonce)
            .replace("__LOCAL_CSRF__", self.csrf)
            .replace("__BRIDGE_URL__", self.bridge_url)
            .encode("utf-8")
        )
        self.lock = threading.Lock()
        self.peer = ""
        self.world = ""
        self.world_size = 0
        self.last_poll = None
        self.players = {}
        self.vehicles = {}
        self.history = []

    def connected(self, now):
        return self.last_poll is not None and now - self.last_poll <= ONLINE_SECONDS

    def age_commands(self, now, changed_peer=False):
        for command in self.history:
            if command.status == "queued":
                if changed_peer or now - command.created_mono > QUEUE_SECONDS:
                    command.status = "expired"
                    command.detail = "mission_changed" if changed_peer else "queue_timeout"
            elif command.status == "sent":
                if changed_peer or now - command.sent_mono > RECEIPT_SECONDS:
                    command.status = "unknown"
                    command.detail = "mission_changed" if changed_peer else "receipt_timeout"

    def poll(self, packet):
        if not equal_secret(packet.get("token"), self.token):
            raise RequestError(HTTPStatus.UNAUTHORIZED, "Ungültiger Verbindungsschlüssel.")
        if type(packet.get("protocol")) is not int or packet["protocol"] != PROTOCOL:
            raise invalid("Unbekannte Protokollversion.")
        session = validated_text(packet.get("session"), "Missions-ID", 30, SESSION_ID)
        world = validated_text(packet.get("world"), "Karte", 80, CLASS_NAME)
        world_size = validated_number(packet.get("worldSize"), "Kartengröße", 100, 100_000)
        players = collect_entities(packet.get("players"), world_size)
        vehicles = collect_entities(packet.get("vehicles"), world_size, vehicles=True)
        receipt_id = packet.get("receiptId")
        if receipt_id != "":
            validated_text(receipt_id, "Auftrags-ID", 24, COMMAND_ID)
            if type(packet.get("receiptOk")) is not bool:
                raise invalid("Die Auftragsquittung ist ungültig.")
            detail = validated_text(packet.get("receiptDetail"), "Auftragsmeldung", 120)

        with self.lock:
            now = time.monotonic()
            changed_peer = bool(self.peer and session != self.peer)
            if changed_peer and self.connected(now):
                raise RequestError(HTTPStatus.CONFLICT, "Eine andere DayZ-Mission ist verbunden.")
            self.age_commands(now, changed_peer)
            self.peer = session
            self.world = world
            self.world_size = world_size
            self.players = players
            self.vehicles = vehicles
            self.last_poll = now

            if receipt_id:
                for command in self.history:
                    if (
                        command.id == receipt_id
                        and command.peer == session
                        and command.status in ("sent", "unknown")
                    ):
                        command.status = "done" if packet["receiptOk"] else "failed"
                        command.detail = detail
                        break

            if not any(command.status == "sent" for command in self.history):
                for command in self.history:
                    if command.status == "queued" and command.peer == session:
                        command.status = "sent"
                        command.sent_mono = now
                        return command.wire()
            return EMPTY_REPLY

    def queue(self, payload):
        action = validated_text(payload.get("action"), "Befehl", 30, CLASS_NAME)
        if action not in ACTIONS:
            raise invalid("Dieser Befehl ist nicht verfügbar.")

        with self.lock:
            now = time.monotonic()
            self.age_commands(now)
            if not self.connected(now):
                raise RequestError(HTTPStatus.CONFLICT, "Die DayZ-Mission ist gerade nicht verbunden.")
            if sum(job.status in ("queued", "sent") for job in self.history) >= MAX_PENDING:
                raise RequestError(HTTPStatus.CONFLICT, "Es warten bereits zu viele Aufträge.")
            arguments, label = self.prepare_command(action, payload)
            command = Command(action, label, self.peer, arguments, time.time(), now)
            if len(self.history) >= MAX_HISTORY:
                finished = next(
                    (job for job in self.history if job.status not in ("queued", "sent")), None
                )
                if finished is not None:
                    self.history.remove(finished)
            self.history.append(command)
            return command.display()

    def prepare_command(self, action, payload):
        args = {}
        label = ACTIONS[action]
        if action in ("notify", "teleport", "give"):
            target = validated_text(payload.get("target"), "Spieler", 128, IDENTIFIER)
            player = self.players.get(target)
            if not player or not player["alive"]:
                raise invalid("Der gewählte Spieler ist nicht mehr verfügbar.")
            if action == "teleport" and player["inVehicle"]:
                raise invalid("Der Spieler muss zum Teleportieren aussteigen.")
            args["target"] = target
            label += " · " + player["name"]
        if action in ("teleport", "spawn_car"):
            args["x"] = validated_number(payload.get("x"), "X", 0, self.world_size)
            args["z"] = validated_number(payload.get("z"), "Z", 0, self.world_size)
            if args["x"] >= self.world_size or args["z"] >= self.world_size:
                raise invalid("Die Zielposition liegt außerhalb der Karte.")
        if action == "notify":
            args["message"] = validated_text(payload.get("message"), "Nachricht", 160)
        elif action == "give":
            item = validated_text(payload.get("typeName"), "Item-Klasse", 80, CLASS_NAME)
            count = payload.get("quantity")
            if item not in ITEMS:
                raise invalid("Diese Item-Klasse ist nicht freigegeben.")
            if type(count) is not int or not 1 <= count <= 8:
                raise invalid("Wähle eine Menge zwischen 1 und 8.")
            args.update(typeName=item, quantity=count)
            label = f"{label} · {count} × {ITEMS[item]}"
        elif action == "spawn_car":
            model = validated_text(payload.get("typeName"), "Fahrzeug-Klasse", 80, CLASS_NAME)
            if model not in CARS:
                raise invalid("Diese Fahrzeug-Klasse ist nicht freigegeben.")
            if len(self.vehicles) >= MAX_VEHICLES:
                raise invalid("Das Limit für Testfahrzeuge ist erreicht.")
            heading = validated_number(payload.get("heading"), "Ausrichtung", 0, 360)
            args.update(typeName=model, heading=heading % 360)
            label += " · " + CARS[model]
        elif action == "remove_car":
            target = validated_text(payload.get("target"), "Fahrzeug", 16, VEHICLE_ID)
            vehicle = self.vehicles.get(target)
            if not vehicle or vehicle["occupied"]:
                raise invalid("Das Testfahrzeug fehlt oder ist besetzt.")
            args["target"] = target
            label += " · " + target
        return args, label

    def dashboard(self):
        with self.lock:
            now = time.monotonic()
            self.age_commands(now)
            age = None if self.last_poll is None else int(now - self.last_poll)
            return {
                "online": self.connected(now),
                "age": age,
                "world": self.world,
                "worldSize": self.world_size,
                "players": list(self.players.values()),
                "vehicles": list(self.vehicles.values()),
                "items": ITEMS,
                "cars": CARS,
                "actions": ACTIONS,
                "commands": [job.display() for job in reversed(self.history)],
            }


class PanelHTTPServer(ThreadingHTTPServer):
    request_queue_size = 24

    def __init__(self, port, state, bridge=False):
        self.state = state
        self.bridge = bridge
        self.request_slots = threading.BoundedSemaphore(20)
        super().__init__(("127.0.0.1", port), RequestHandler)

    def process_request(self, request, client_address):
        if not self.request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            request.settimeout(5)
            super().process_request(request, client_address)
        except BaseException:
            self.request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()

    def handle_error(self, request, client_address):
        # Never include request contents in the operator log.
        pass


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "Brigarde"
    sys_version = ""

    def log_message(self, format, *args):
        pass

    def send_payload(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        if content_type.startswith("text/html"):
            nonce = self.server.state.nonce
            self.send_header(
                "Content-Security-Policy",
                f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
            )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        self.send_payload(status, body.encode("utf-8"), "application/json; charset=utf-8")

    def verify_local(self, write=False):
        port = self.server.server_port
        valid_hosts = (f"127.0.0.1:{port}", f"localhost:{port}")
        host = self.headers.get("Host")
        if host not in valid_hosts:
            raise RequestError(HTTPStatus.FORBIDDEN, "Das Panel verwendet eine lokale Adresse.")
        if write:
            origin = self.headers.get("Origin")
            if (origin is not None and origin != f"http://{host}") or not equal_secret(
                self.headers.get("X-Panel-CSRF"), self.server.state.csrf
            ):
                raise RequestError(HTTPStatus.FORBIDDEN, "Die lokale Anfrage ist nicht autorisiert.")

    def read_json(self):
        if (
            self.headers.get("Transfer-Encoding")
            or self.headers.get_content_type() != "application/json"
        ):
            raise invalid("Erwartet wird ein begrenzter JSON-Request.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise invalid("Ungültige Request-Größe.") from exc
        if not 1 <= length <= MAX_BODY_BYTES:
            raise RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Die Request-Größe ist ungültig.")
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError
            data = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise invalid("Ungültige JSON-Daten.") from exc
        if not isinstance(data, dict):
            raise invalid("Erwartet wird ein JSON-Objekt.")
        return data

    def do_GET(self):
        try:
            if self.server.bridge:
                if self.path == "/health":
                    return self.send_json(HTTPStatus.OK, {"protocol": PROTOCOL})
            else:
                self.verify_local()
                if self.path == "/":
                    return self.send_payload(HTTPStatus.OK, self.server.state.page, "text/html; charset=utf-8")
                if self.path == "/local/state":
                    return self.send_json(HTTPStatus.OK, self.server.state.dashboard())
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Route nicht gefunden."})
        except RequestError as exc:
            self.send_json(exc.status, {"error": exc.message})

    def do_POST(self):
        try:
            if self.server.bridge and self.path == "/dayz/poll":
                return self.send_json(HTTPStatus.OK, self.server.state.poll(self.read_json()))
            if not self.server.bridge and self.path in ("/local/config", "/local/commands"):
                self.verify_local(write=True)
                payload = self.read_json()
                if self.path == "/local/commands":
                    return self.send_json(HTTPStatus.CREATED, self.server.state.queue(payload))
                address = bridge_origin(payload.get("bridgeUrl"), self.server.state.bridge_url)
                return self.send_json(
                    HTTPStatus.OK,
                    {"protocol": PROTOCOL, "baseUrl": address, "token": self.server.state.token},
                )
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Route nicht gefunden."})
        except RequestError as exc:
            self.send_json(exc.status, {"error": exc.message})
        except socket.timeout:
            self.send_json(HTTPStatus.REQUEST_TIMEOUT, {"error": "Zeitlimit beim Lesen erreicht."})


def main():
    parser = argparse.ArgumentParser(description="Brigarde Init Panel für DayZ-Testmissionen")
    parser.add_argument("--panel-port", type=int, default=8787)
    parser.add_argument("--bridge-port", type=int, default=8788)
    parser.add_argument("--state-dir", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    ports = {args.panel_port, args.bridge_port}
    if len(ports) != 2 or any(not 1024 <= port <= 65535 for port in ports):
        parser.error("Wähle zwei unterschiedliche Ports zwischen 1024 und 65535.")
    try:
        state = PanelState(read_bridge_key(args.state_dir), args.bridge_port, PANEL_HTML)
        with PanelHTTPServer(args.panel_port, state) as panel:
            with PanelHTTPServer(args.bridge_port, state, bridge=True) as bridge:
                thread = threading.Thread(target=bridge.serve_forever, daemon=True)
                thread.start()
                print(f"Panel   http://127.0.0.1:{args.panel_port}", flush=True)
                print(f"Bridge  {state.bridge_url}/health", flush=True)
                try:
                    panel.serve_forever()
                except KeyboardInterrupt:
                    pass
                finally:
                    bridge.shutdown()
                    thread.join()
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Start fehlgeschlagen. {exc}\n")


# Bundle the page so launchers such as Pydroid can run a copy of this script alone.
PANEL_HTML = r"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="panel-csrf" content="__LOCAL_CSRF__">
<title>Brigarde Init Panel</title>
<style nonce="__PANEL_NONCE__">
:root {
  color-scheme: dark;
  --bg: #101415;
  --panel: #181e20;
  --raised: #222a2c;
  --line: #30393b;
  --text: #eaf0e9;
  --muted: #a1b0ac;
  --accent: #cbe8aa;
  --gold: #f7c878;
  --danger: #ffad9d;
  font-family: "Inter", "Segoe UI", system-ui, sans-serif;
}
* { box-sizing: border-box; }
body { margin: 0; min-width: 300px; background: var(--bg); color: var(--text); }
button, input, select { font: inherit; }
[hidden] { display: none !important; }
.topbar { border-bottom: 1px solid var(--line); background: #151a1c; }
.top-inner {
  width: min(1334px, 100% - 48px); margin: auto; min-height: 76px;
  display: flex; align-items: center; justify-content: space-between; gap: 14px;
}
.brand, .top-actions, .connection, .heading { display: flex; align-items: center; gap: 12px; }
.brand-mark {
  width: 42px; height: 42px; flex: 0 0 42px; display: grid; place-items: center;
  border: 1px solid #46543b; border-radius: 12px; background: #263327;
  color: var(--accent); font: 650 17px "IBM Plex Mono", ui-monospace, monospace;
}
h1, h2, p { margin: 0; }
h1 { font-size: 20px; letter-spacing: -.035em; line-height: 1.17; }
.brand small { color: var(--muted); font-size: 12px; }
.connection { font-size: 13px; gap: 7px; color: var(--muted); white-space: nowrap; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--gold); }
.connection[data-online="true"] { color: var(--accent); }
.connection[data-online="true"] .dot { background: var(--accent); }
main { width: min(1334px, 100% - 48px); margin: 28px auto 46px; }
.metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 18px; }
.metric, .card { border: 1px solid var(--line); border-radius: 14px; background: var(--panel); min-width: 0; }
.metric { padding: 14px 17px; display: grid; gap: 6px; }
.metric span { font-size: 12px; color: var(--muted); }
.metric b { font-weight: 630; font-size: 20px; line-height: 1.2; overflow-wrap: anywhere; }
.workspace { display: grid; grid-template-columns: minmax(0, 1.55fr) minmax(310px, 1fr); gap: 18px; }
.card { padding: 18px; }
.heading { justify-content: space-between; gap: 8px; margin-bottom: 13px; }
.heading h2 { font-size: 16px; font-weight: 660; letter-spacing: -.014em; }
.heading small, .hint { color: var(--muted); font-size: 12px; line-height: 1.55; }
.map {
  position: relative; overflow: hidden; min-height: 346px; aspect-ratio: 1.58;
  border: 1px solid var(--line); border-radius: 10px; background: #141a1b;
}
canvas { position: absolute; inset: 0; width: 100%; height: 100%; display: block; cursor: crosshair; }
.map-empty {
  position: absolute; inset: 0; display: grid; align-content: center; justify-items: center;
  gap: 12px; padding: 18px; background: #151b1c; font-size: 14px; text-align: center;
}
.map-foot { margin-top: 10px; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
.legend { display: flex; flex-wrap: wrap; column-gap: 14px; row-gap: 4px; }
.legend span { display: flex; gap: 6px; align-items: center; }
.legend i { width: 7px; height: 7px; border-radius: 50%; background: var(--accent); }
.legend .vehicle-dot { border-radius: 2px; background: var(--gold); }
form, .fields { display: grid; gap: 12px; }
.two { grid-template-columns: repeat(2, minmax(0, 1fr)); }
label { display: grid; min-width: 0; gap: 6px; font-size: 13px; color: var(--muted); }
button, input, select {
  min-height: 40px; border: 1px solid #3b4749; border-radius: 8px;
  color: var(--text); background: var(--raised); padding: 9px 11px;
}
input, select { width: 100%; }
button { cursor: pointer; width: fit-content; font-weight: 580; font-size: 13px; }
button:hover { border-color: #82948b; }
button:focus-visible, input:focus-visible, select:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
button:disabled, select:disabled { opacity: .58; cursor: default; }
.primary { background: var(--accent); color: #172114; border-color: var(--accent); }
.primary:hover:enabled { background: #d8f4ba; color: #172114; }
.submit { width: 100%; margin-top: 2px; }
.feedback { min-height: 18px; color: var(--accent); font-size: 12px; line-height: 1.4; overflow-wrap: anywhere; }
.feedback[data-error="true"] { color: var(--danger); }
.entities { margin-top: 18px; }
.entity-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
.table-scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; white-space: nowrap; }
td, th { padding: 9px 10px 9px 0; text-align: left; }
th { border-bottom: 1px solid var(--line); color: var(--muted); font-size: 12px; font-weight: 600; }
tbody tr + tr { border-top: 1px solid #2a3234; }
td:first-child { white-space: normal; overflow-wrap: anywhere; min-width: 100px; }
td:last-child, th:last-child { padding-right: 2px; }
td.coord { font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 12px; }
td[data-status="done"] { color: var(--accent); }
td[data-status="unknown"] { color: var(--gold); }
td[data-status="failed"], td[data-status="expired"] { color: var(--danger); }
.history { margin-top: 18px; }
dialog { width: min(490px, calc(100% - 24px)); border: 1px solid #465255; border-radius: 14px; padding: 20px; background: var(--panel); color: var(--text); }
dialog::backdrop { background: #0a0d0de0; }
dialog form { margin-top: 14px; }
code { color: var(--accent); font-family: "IBM Plex Mono", ui-monospace, monospace; overflow-wrap: anywhere; }
@media (max-width: 840px) {
  .workspace { grid-template-columns: 1fr; }
  .map { aspect-ratio: 1.7; min-height: 295px; }
  .top-inner, main { width: calc(100% - 32px); }
}
@media (max-width: 540px) {
  .top-inner { min-height: 66px; gap: 8px; }
  .brand { gap: 8px; }
  h1 { font-size: 17px; }
  .brand-mark { width: 35px; height: 35px; flex-basis: 35px; }
  .top-actions { gap: 8px; }
  .connection { font-size: 0; }
  .top-inner, main { width: calc(100% - 24px); }
  main { margin-top: 18px; }
  .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
  .metric { padding: 10px 12px; }
  .card { padding: 13px; }
  .entity-grid { grid-template-columns: 1fr; }
  .map { min-height: 255px; }
  .workspace, .entity-grid { gap: 12px; }
}
</style>
</head>
<body>
<header class="topbar">
  <div class="top-inner">
    <div class="brand">
      <div class="brand-mark" aria-hidden="true">B</div>
      <div>
        <h1>Brigarde Init Panel</h1>
        <small>DayZ-Missionsverwaltung</small>
      </div>
    </div>
    <div class="top-actions">
      <span class="connection" id="connection" role="status" aria-live="polite"><i class="dot"></i><span id="connection-text">Warte auf DayZ</span></span>
      <button type="button" id="setup-button">Verbindung</button>
    </div>
  </div>
</header>
<main>
  <section class="metrics" aria-label="Missionsstatus">
    <div class="metric"><span>Karte</span><b id="world">–</b></div>
    <div class="metric"><span>Spieler online</span><b id="player-count">–</b></div>
    <div class="metric"><span>Testfahrzeuge</span><b id="car-count">–</b></div>
    <div class="metric"><span>Letzter Abruf</span><b id="age">–</b></div>
  </section>
  <div class="workspace">
    <section class="card" aria-labelledby="map-title">
      <div class="heading"><h2 id="map-title">Koordinatenkarte</h2><small id="world-size"></small></div>
      <div class="map">
        <canvas id="map" aria-label="Positionen von Spielern und Testfahrzeugen. Koordinaten lassen sich auch im Befehlsformular eingeben."></canvas>
        <div class="map-empty" id="map-empty">
          <p>Warte auf den ersten Abruf der DayZ-Mission.</p>
          <button type="button" id="map-setup-button">Verbindung einrichten</button>
        </div>
      </div>
      <div class="map-foot hint">
        <div class="legend"><span><i></i>Spieler</span><span><i class="vehicle-dot"></i>Testfahrzeug</span></div>
        <span id="map-hint">X und Z per Klick übernehmen</span>
      </div>
    </section>
    <section class="card" aria-labelledby="command-title">
      <div class="heading"><h2 id="command-title">Befehl ausführen</h2></div>
      <form id="command-form">
        <label>Aktion
          <select id="action" aria-label="Aktion"></select>
        </label>
        <label data-show="notify teleport give" hidden>Spieler
          <select id="target-player" aria-label="Spieler"></select>
        </label>
        <label data-show="notify" hidden>Nachricht
          <input id="notice" type="text" maxlength="160" autocomplete="off" placeholder="Nachricht an den Spieler">
        </label>
        <div class="fields two" data-show="teleport spawn_car" hidden>
          <label>Position X<input id="x" type="number" min="0" step="any" inputmode="decimal"></label>
          <label>Position Z<input id="z" type="number" min="0" step="any" inputmode="decimal"></label>
        </div>
        <label data-show="give" hidden>Item
          <select id="item-type" aria-label="Item"></select>
        </label>
        <label data-show="give" hidden>Menge
          <input id="quantity" type="number" min="1" max="8" step="1" value="1">
        </label>
        <label data-show="spawn_car" hidden>Fahrzeugtyp
          <select id="car-type" aria-label="Fahrzeugtyp"></select>
        </label>
        <label data-show="spawn_car" hidden>Ausrichtung in Grad
          <input id="heading" type="number" min="0" max="360" step="any" value="0">
        </label>
        <label data-show="remove_car" hidden>Testfahrzeug
          <select id="target-car" aria-label="Testfahrzeug"></select>
        </label>
        <p class="hint" id="action-hint">Mit einem Testbefehl die Antwort der Mission prüfen.</p>
        <button type="submit" class="primary submit" id="send" disabled>Befehl senden</button>
        <p class="feedback" id="command-feedback" role="status"></p>
      </form>
    </section>
  </div>
  <div class="entity-grid entities">
    <section class="card" aria-labelledby="players-title">
      <div class="heading"><h2 id="players-title">Spieler</h2></div>
      <div class="table-scroll">
        <table>
          <thead><tr><th scope="col">Name</th><th scope="col">X</th><th scope="col">Y</th><th scope="col">Z</th><th scope="col">Status</th></tr></thead>
          <tbody id="players"><tr><td colspan="5">Noch keine Mission verbunden</td></tr></tbody>
        </table>
      </div>
    </section>
    <section class="card" aria-labelledby="vehicles-title">
      <div class="heading"><h2 id="vehicles-title">Testfahrzeuge</h2></div>
      <div class="table-scroll">
        <table>
          <thead><tr><th scope="col">Fahrzeug</th><th scope="col">X</th><th scope="col">Z</th><th scope="col">Status</th></tr></thead>
          <tbody id="vehicles"><tr><td colspan="4">Noch keine Mission verbunden</td></tr></tbody>
        </table>
      </div>
    </section>
  </div>
  <section class="card history" aria-labelledby="history-title">
    <div class="heading"><h2 id="history-title">Aufträge</h2></div>
    <div class="table-scroll">
      <table>
        <thead><tr><th scope="col">Auftrag</th><th scope="col">Status</th><th scope="col">Ergebnis</th><th scope="col">Uhrzeit</th></tr></thead>
        <tbody id="history"><tr><td colspan="4">Noch keine Aufträge</td></tr></tbody>
      </table>
    </div>
  </section>
</main>
<dialog id="setup-dialog" aria-labelledby="setup-title">
  <div class="heading">
    <h2 id="setup-title">Verbindung zur Mission</h2>
    <button type="button" id="close-setup">Schließen</button>
  </div>
  <p class="hint">Läuft der DayZ-Testserver auf demselben Gerät, verwende die lokale Vorgabe. Für einen externen Server verwende die HTTPS-Adresse des Tunnels zur Bridge.</p>
  <form id="config-form">
    <label>Bridge-Adresse
      <input id="bridge-url" type="url" required value="__BRIDGE_URL__" spellcheck="false" autocomplete="off">
    </label>
    <button type="submit" id="download-config" class="primary">Missionskonfiguration herunterladen</button>
    <p class="hint">Die Datei <code>BrigardeInitPanel.json</code> kommt in <code>custom/</code> deiner Testmission. Sie enthält den Verbindungsschlüssel.</p>
    <p class="feedback" id="config-feedback" role="status"></p>
  </form>
</dialog>
<script nonce="__PANEL_NONCE__">
"use strict";

const byId = id => document.getElementById(id);
const csrf = document.querySelector('meta[name="panel-csrf"]').content;
const canvas = byId("map");
const context = canvas.getContext("2d");
const form = byId("command-form");
const actionSelect = byId("action");
const setupDialog = byId("setup-dialog");
const positions = ["teleport", "spawn_car"];
const playerActions = ["notify", "teleport", "give"];
const coordinate = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 1 });
const clock = new Intl.DateTimeFormat("de-DE", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const worldNames = { nasdara: "Nasdara", chernarusplus: "Chernarus", enoch: "Livonia", sakhal: "Sakhal" };
const stateLabels = { queued: "Wartet", sent: "Übermittelt", done: "Ausgeführt", failed: "Fehlgeschlagen", expired: "Abgelaufen", unknown: "Ausführung offen" };
const actionHints = {
  ping: "Der Befehl wartet auf die Rückmeldung des Missionsskripts.",
  notify: "Sendet eine Ingame-Benachrichtigung an den gewählten Spieler.",
  teleport: "Wähle offene Fläche. Die Mission ermittelt die Höhe des Geländes.",
  give: "Legt die gewählte Menge in freie Inventarplätze.",
  spawn_car: "Wähle eine freie Fläche für ein ausgestattetes Testfahrzeug.",
  remove_car: "Die Mission prüft, ob das gewählte Testfahrzeug frei ist."
};
const detailLabels = {
  pong: "Antwort der Mission erhalten", notified: "Spieler benachrichtigt",
  teleported: "Position am Server gesetzt", car_removed: "Testfahrzeug entfernt",
  player_missing: "Spieler inzwischen nicht mehr verfügbar", player_busy: "Spieler sitzt im Fahrzeug oder klettert",
  invalid_ground: "Ungültige Position oder Wasserfläche", invalid_item: "Ungültige Item-Klasse oder Menge",
  invalid_vehicle: "Fahrzeugklasse, Winkel oder Fahrzeuglimit ungültig", invalid_message: "Nachricht ist ungültig",
  spawn_occupied: "Spieler oder Fahrzeug zu dicht am Ziel", spawn_failed: "DayZ konnte das Fahrzeug nicht erzeugen",
  vehicle_missing: "Testfahrzeug nicht mehr vorhanden", vehicle_occupied: "Testfahrzeug ist besetzt",
  receipt_timeout: "Keine Rückmeldung der Mission", queue_timeout: "Auftrag wurde nicht abgeholt",
  mission_changed: "Mission wurde gewechselt", unsupported: "Befehl in dieser Mission unbekannt"
};
let current = null;
let reachable = true;
let sending = false;
let selected = null;
let layout = null;
let markers = [];

function detailText(detail) {
  if (!detail) return "–";
  const [kind, value] = detail.split(":", 2);
  if (kind === "items_given") return value + " Item(s) erzeugt";
  if (kind === "item_partial") return value + " Item(s) erzeugt · Inventar voll oder Klasse nicht verfügbar";
  if (kind === "car_spawned") return "Fahrzeug " + value + " erzeugt";
  return detailLabels[kind] || detail;
}

async function request(path, payload) {
  const options = payload === undefined ? { cache: "no-store" } : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Panel-CSRF": csrf },
    body: JSON.stringify(payload)
  };
  const response = await fetch(path, options);
  const json = await response.json();
  if (!response.ok) throw new Error(json.error || "Die Anfrage ist fehlgeschlagen.");
  return json;
}

function options(select, entries, emptyLabel) {
  const previous = select.value;
  const list = entries.length ? entries : [["", emptyLabel]];
  const signature = JSON.stringify(list);
  if (select.dataset.signature === signature) return;
  select.dataset.signature = signature;
  select.replaceChildren(...list.map(([value, label]) => new Option(label, value)));
  if (entries.some(([key]) => key === previous)) select.value = previous;
}

function showMessage(id, message, failed = false) {
  const element = byId(id);
  element.textContent = message;
  element.dataset.error = String(failed);
}

function updateForm() {
  const action = actionSelect.value;
  for (const group of form.querySelectorAll("[data-show]")) {
    const visible = group.dataset.show.split(" ").includes(action);
    group.hidden = !visible;
    for (const input of group.querySelectorAll("input,select")) {
      input.disabled = !visible;
      input.required = visible;
    }
  }
  byId("action-hint").textContent = actionHints[action] || actionHints.ping;
  const missingPlayer = playerActions.includes(action) && !byId("target-player").value;
  const missingCar = action === "remove_car" && !byId("target-car").value;
  byId("send").disabled = sending || !reachable || !current?.online || missingPlayer || missingCar;
}

function tableRows(id, rows, columnCount, emptyLabel) {
  const body = byId(id);
  if (!rows.length) {
    const row = document.createElement("tr");
    const cell = row.insertCell();
    cell.colSpan = columnCount;
    cell.textContent = emptyLabel;
    body.replaceChildren(row);
    return;
  }
  body.replaceChildren(...rows.map(cells => {
    const row = document.createElement("tr");
    cells.forEach(value => {
      const cell = row.insertCell();
      if (typeof value === "number") {
        cell.className = "coord";
        cell.textContent = coordinate.format(value);
      } else if (typeof value === "object") {
        cell.textContent = value.text;
        cell.dataset.status = value.status;
      } else {
        cell.textContent = value;
      }
    });
    return row;
  }));
}

function render() {
  const online = reachable && current?.online;
  byId("connection").dataset.online = String(Boolean(online));
  const connectionText = !reachable ? "Panel nicht erreichbar"
    : online ? "Mission verbunden" : current?.age != null ? "Mission getrennt" : "Warte auf DayZ";
  if (byId("connection-text").textContent !== connectionText) byId("connection-text").textContent = connectionText;
  if (!current) {
    updateForm();
    return;
  }
  byId("world").textContent = worldNames[current.world.toLowerCase()] || current.world || "–";
  byId("player-count").textContent = online ? current.players.length : "–";
  byId("car-count").textContent = online ? current.vehicles.length : "–";
  byId("age").textContent = current.age === null ? "–" : "vor " + current.age + " s";
  byId("world-size").textContent = current.worldSize ? coordinate.format(current.worldSize) + " m je Achse" : "";
  options(actionSelect, Object.entries(current.actions), "Aktion wählen");
  options(byId("item-type"), Object.entries(current.items), "Item wählen");
  options(byId("car-type"), Object.entries(current.cars), "Fahrzeug wählen");
  options(byId("target-player"), current.players.filter(player => player.alive).map(player => [player.id, player.name]), "Keine lebenden Spieler");
  options(byId("target-car"), current.vehicles.filter(car => !car.occupied).map(car => [car.id, (current.cars[car.typeName] || car.typeName) + " · " + car.id]), "Keine freien Testfahrzeuge");

  tableRows("players", current.players.map(player => [
    player.name, player.x, player.y, player.z, !player.alive ? "Tot" : player.inVehicle ? "Im Fahrzeug" : "Zu Fuß"
  ]), 5, current.age === null ? "Noch keine Mission verbunden" : "Keine Spieler im letzten Abruf");
  tableRows("vehicles", current.vehicles.map(car => [
    (current.cars[car.typeName] || car.typeName) + " · " + car.id, car.x, car.z, car.occupied ? "Besetzt" : "Frei"
  ]), 4, current.age === null ? "Noch keine Mission verbunden" : "Keine Testfahrzeuge im letzten Abruf");
  tableRows("history", current.commands.map(job => [
    job.label, { text: stateLabels[job.status], status: job.status }, detailText(job.detail), clock.format(job.created * 1000)
  ]), 4, "Noch keine Aufträge");
  updateForm();
  drawMap();
}

function drawMap() {
  const box = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  const width = Math.round(box.width);
  const height = Math.round(box.height);
  if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
  }
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  byId("map-empty").hidden = Boolean(current?.worldSize);
  markers = [];
  layout = null;
  if (!current?.worldSize || width < 90 || height < 90) return;

  const size = Math.min(width - 80, height - 44);
  const left = (width - size) / 2;
  const top = (height - size) / 2 - 5;
  const span = current.worldSize;
  layout = { left, top, size, span };
  context.fillStyle = "#1b2724";
  context.fillRect(left, top, size, size);
  context.strokeStyle = "#364742";
  context.fillStyle = "#9baca5";
  context.font = "10px ui-monospace, monospace";
  for (let index = 0; index <= 6; index++) {
    const offset = size * index / 6;
    const caption = Math.round(span * index / 6);
    context.beginPath();
    context.moveTo(left + offset, top);
    context.lineTo(left + offset, top + size);
    context.moveTo(left, top + size - offset);
    context.lineTo(left + size, top + size - offset);
    context.stroke();
    if (index % 2 === 0) {
      context.textAlign = "center";
      context.fillText(caption, left + offset, top + size + 15);
      context.textAlign = "right";
      context.fillText(caption, left - 6, top + size - offset + 3);
    }
  }
  context.textAlign = "left";
  context.fillText("X", left + size + 12, top + size + 15);
  context.fillText("Z", left - 25, top - 7);

  const plot = (entry, color, kind, label) => {
    if (entry.x < 0 || entry.z < 0 || entry.x >= span || entry.z >= span) return;
    const x = left + size * entry.x / span;
    const y = top + size * (1 - entry.z / span);
    context.fillStyle = color;
    context.strokeStyle = "#101415";
    context.lineWidth = 2;
    context.beginPath();
    if (kind === "car") context.rect(x - 4, y - 4, 8, 8);
    else context.arc(x, y, 4.5, 0, 2 * Math.PI);
    context.fill();
    context.stroke();
    context.globalAlpha = .9;
    context.textAlign = x > left + size * .74 ? "right" : "left";
    context.fillText(label.slice(0, 20), x + (context.textAlign === "right" ? -7 : 7), y - 5);
    context.globalAlpha = 1;
    markers.push({ x, y, id: entry.id, kind });
  };

  for (const car of current.vehicles) plot(car, current.online ? "#f7c878" : "#9e8d71", "car", car.id);
  for (const player of current.players) plot(player, current.online && player.alive ? "#cbe8aa" : "#92a099", "player", player.name);
  if (selected && selected.x < span && selected.z < span) {
    const x = left + size * selected.x / span;
    const y = top + size * (1 - selected.z / span);
    context.strokeStyle = "#a9ceff";
    context.lineWidth = 1;
    context.beginPath();
    context.moveTo(x - 7, y);
    context.lineTo(x + 7, y);
    context.moveTo(x, y - 7);
    context.lineTo(x, y + 7);
    context.stroke();
  }
}

canvas.addEventListener("click", event => {
  if (!layout || !current.online) return;
  const rect = canvas.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const y = event.clientY - rect.top;
  const { left, top, size, span } = layout;
  if (x < left || y < top || x >= left + size || y >= top + size) return;
  const hit = markers.find(marker => Math.hypot(x - marker.x, y - marker.y) <= 7);
  if (hit) {
    if (hit.kind === "car" && actionSelect.value === "remove_car") byId("target-car").value = hit.id;
    if (hit.kind === "player" && playerActions.includes(actionSelect.value)) byId("target-player").value = hit.id;
  }
  selected = {
    x: Math.min(span - .1, Math.round((x - left) / size * span * 10) / 10),
    z: Math.min(span - .1, Math.round((1 - (y - top) / size) * span * 10) / 10)
  };
  byId("x").value = selected.x;
  byId("z").value = selected.z;
  byId("map-hint").textContent = "X " + coordinate.format(selected.x) + " · Z " + coordinate.format(selected.z);
  updateForm();
  drawMap();
});

async function refresh() {
  try {
    current = await request("/local/state");
    reachable = true;
  } catch {
    reachable = false;
  }
  render();
}

async function refreshCycle() {
  await refresh();
  window.setTimeout(refreshCycle, 1500);
}

form.addEventListener("submit", async event => {
  event.preventDefault();
  if (byId("send").disabled) return;
  const action = actionSelect.value;
  const payload = { action };
  if (playerActions.includes(action)) payload.target = byId("target-player").value;
  if (action === "notify") payload.message = byId("notice").value.trim();
  if (positions.includes(action)) {
    payload.x = byId("x").valueAsNumber;
    payload.z = byId("z").valueAsNumber;
  }
  if (action === "give") {
    payload.typeName = byId("item-type").value;
    payload.quantity = byId("quantity").valueAsNumber;
  }
  if (action === "spawn_car") {
    payload.typeName = byId("car-type").value;
    payload.heading = byId("heading").valueAsNumber;
  }
  if (action === "remove_car") payload.target = byId("target-car").value;
  sending = true;
  updateForm();
  try {
    await request("/local/commands", payload);
    showMessage("command-feedback", "Auftrag hinzugefügt. Die Rückmeldung erscheint unten.");
  } catch (error) {
    showMessage("command-feedback", error.message, true);
  } finally {
    sending = false;
    updateForm();
  }
});

actionSelect.addEventListener("change", () => {
  showMessage("command-feedback", "");
  updateForm();
});
byId("target-player").addEventListener("change", updateForm);
byId("target-car").addEventListener("change", updateForm);
const openSetup = () => setupDialog.showModal();
byId("setup-button").addEventListener("click", openSetup);
byId("map-setup-button").addEventListener("click", openSetup);
byId("close-setup").addEventListener("click", () => setupDialog.close());

byId("config-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = byId("download-config");
  button.disabled = true;
  try {
    const config = await request("/local/config", { bridgeUrl: byId("bridge-url").value });
    const href = URL.createObjectURL(new Blob([JSON.stringify(config, null, 2) + "\n"], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = href;
    link.download = "BrigardeInitPanel.json";
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(href), 1000);
    showMessage("config-feedback", "Konfiguration erstellt.");
  } catch (error) {
    showMessage("config-feedback", error.message, true);
  } finally {
    button.disabled = false;
  }
});

new ResizeObserver(drawMap).observe(canvas);
refreshCycle();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
