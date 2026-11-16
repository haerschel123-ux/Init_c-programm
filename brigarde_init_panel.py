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
        markup = (ROOT / "panel.html").read_text(encoding="utf-8")
        state = PanelState(read_bridge_key(args.state_dir), args.bridge_port, markup)
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


if __name__ == "__main__":
    main()
