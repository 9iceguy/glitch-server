"""TCP server for multiplayer state, pickups, purchases, and match stats."""

import json
import os
import random
import sqlite3
import socket
import threading
import time
import uuid
from contextlib import closing

ADDR = "0.0.0.0"  # was "localhost"

PORT = 8000
MAX_PLAYERS = 20
MSG_SIZE = 2048
AMMO_COUNTDOWN = 60
MED_COUNTDOWN = 120
CASH_SPAWN_INTERVAL = 45
CASH_LOOT_LIMIT = 12
MAP_BOUNDS = (-110, 110)
STARTING_CASH = 100
KILL_REWARD = 50
VENDING_MACHINE_POSITION = (16, 4.2, -16)
VENDING_MACHINE_DISTANCE = 5
CASH_PICKUP_DISTANCE = 4

SHOP_CATALOG = {
    "ammo_smg": {"label": "SMG ammo (+80)", "price": 25, "ammo_type": "smg", "amount": 80},
    "ammo_grenade": {"label": "Grenades (+3)", "price": 40, "ammo_type": "grenade", "amount": 3},
    "ammo_knife": {"label": "Throwing knives (+5)", "price": 20, "ammo_type": "knife", "amount": 5},
    "ammo_arrow": {"label": "Arrows (+10)", "price": 30, "ammo_type": "arrow", "amount": 10},
    "ammo_shield": {"label": "Shield charges (+2)", "price": 60, "ammo_type": "shield", "amount": 2},
    "ammo_sniper": {"label": "Sniper ammo (+5)", "price": 50, "ammo_type": "sniper", "amount": 5},
    "health": {"label": "Health (+60)", "price": 40, "amount": 60},
}

players = {}
world_voxels = set()
cash_loot = {}
state_lock = threading.RLock()
profile_lock = threading.Lock()
PROFILE_DATABASE = os.path.join(os.path.dirname(__file__), "profiles.sqlite3")


def initialize_profiles():
    with profile_lock, closing(sqlite3.connect(PROFILE_DATABASE)) as database:
        with database:
            database.execute(
                """
                CREATE TABLE IF NOT EXISTS profiles (
                    username_key TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    kills INTEGER NOT NULL DEFAULT 0,
                    deaths INTEGER NOT NULL DEFAULT 0,
                    matches INTEGER NOT NULL DEFAULT 0
                )
                """
            )


def load_profile(username):
    username_key = username.casefold()
    with profile_lock, closing(sqlite3.connect(PROFILE_DATABASE)) as database:
        with database:
            database.execute(
                """
                INSERT INTO profiles (username_key, username, matches)
                VALUES (?, ?, 1)
                ON CONFLICT(username_key) DO UPDATE SET
                    username = excluded.username,
                    matches = profiles.matches + 1
                """,
                (username_key, username),
            )
    return profile_data(username)


def profile_data(username):
    with profile_lock, closing(sqlite3.connect(PROFILE_DATABASE)) as database:
        row = database.execute(
            """
            SELECT username, kills, deaths, matches
            FROM profiles
            WHERE username_key = ?
            """,
            (username.casefold(),),
        ).fetchone()
    if row is None:
        raise LookupError(f"Profile not found for {username!r}.")
    return {
        "username": row[0],
        "kills": row[1],
        "deaths": row[2],
        "matches": row[3],
        "kd": row[1] / row[2] if row[2] else float(row[1]),
    }


def record_profile_result(attacker_username, victim_username):
    with profile_lock, closing(sqlite3.connect(PROFILE_DATABASE)) as database:
        with database:
            if attacker_username is not None:
                database.execute(
                    "UPDATE profiles SET kills = kills + 1 WHERE username_key = ?",
                    (attacker_username.casefold(),),
                )
            database.execute(
                "UPDATE profiles SET deaths = deaths + 1 WHERE username_key = ?",
                (victim_username.casefold(),),
            )


def send_json(conn, data, send_lock=None):
    payload = (json.dumps(data) + "\n").encode("utf8")
    if send_lock is None:
        conn.sendall(payload)
        return
    with send_lock:
        conn.sendall(payload)


def send_to_player(identifier, data):
    with state_lock:
        player = players.get(identifier)
    if player is None:
        return False
    try:
        send_json(player["socket"], data, player["send_lock"])
        return True
    except OSError as error:
        print(f"Could not send message to player {identifier}: {error}")
        return False


def broadcast(data, exclude=None):
    with state_lock:
        recipients = [
            (identifier, player["socket"], player["send_lock"])
            for identifier, player in players.items()
            if identifier != exclude
        ]
    for identifier, conn, send_lock in recipients:
        try:
            send_json(conn, data, send_lock)
        except OSError as error:
            print(f"Could not send message to player {identifier}: {error}")


def leaderboard_data():
    with state_lock:
        rows = [
            {
                "id": identifier,
                "username": player["username"],
                "kills": player["kills"],
                "deaths": player["deaths"],
                "kd": (
                    player["kills"] / player["deaths"]
                    if player["deaths"]
                    else float(player["kills"])
                ),
            }
            for identifier, player in players.items()
        ]
    rows.sort(key=lambda row: (-row["kd"], -row["kills"], row["username"].casefold()))
    return {"object": "leaderboard", "players": rows}


def broadcast_leaderboard():
    broadcast(leaderboard_data())


def send_countdowns(object_name, duration):
    for seconds in range(duration, 0, -1):
        broadcast({"object": object_name, "time": seconds})
        time.sleep(1)


def ammo_spawner():
    while True:
        send_countdowns("ammo_countdown", AMMO_COUNTDOWN)
        broadcast({
            "object": "ammo",
            "position": (
                random.randint(*MAP_BOUNDS),
                1,
                random.randint(*MAP_BOUNDS),
            ),
            "ammo": random.randint(20, 30),
            "type": random.choice(["smg", "grenade", "sniper", "arrow", "shield", "knife"]),
        })
        time.sleep(AMMO_COUNTDOWN)


def medkit_spawner():
    while True:
        send_countdowns("med_countdown", MED_COUNTDOWN)
        broadcast({
            "object": "med",
            "position": (
                random.randint(*MAP_BOUNDS),
                1,
                random.randint(*MAP_BOUNDS),
            ),
            "hp": random.choice([4, 5]),
        })
        time.sleep(MED_COUNTDOWN)


def cash_spawner():
    while True:
        time.sleep(CASH_SPAWN_INTERVAL)
        pickup = {
            "object": "cash",
            "id": uuid.uuid4().hex,
            "position": (
                random.randint(*MAP_BOUNDS),
                1,
                random.randint(*MAP_BOUNDS),
            ),
            "amount": random.randint(25, 75),
        }
        with state_lock:
            expired_ids = []
            while len(cash_loot) >= CASH_LOOT_LIMIT:
                expired_id = next(iter(cash_loot))
                del cash_loot[expired_id]
                expired_ids.append(expired_id)
            cash_loot[pickup["id"]] = pickup
        for expired_id in expired_ids:
            broadcast({"object": "cash_removed", "id": expired_id})
        broadcast(pickup)


def generate_id(player_list, max_players):
    available_ids = set(map(str, range(1, max_players + 1)))
    available_ids.difference_update(player_list)
    return random.choice(tuple(available_ids)) if available_ids else None


def distance_squared(first, second, axes=(0, 1, 2)):
    try:
        return sum((float(first[index]) - float(second[index])) ** 2 for index in axes)
    except (IndexError, TypeError, ValueError):
        return None


def process_health_update(attacker_id, message):
    target_id = str(message.get("id", ""))
    try:
        reported_health = max(0, min(200, int(message["health"])))
    except (KeyError, TypeError, ValueError):
        return

    with state_lock:
        target = players.get(target_id)
        attacker = players.get(attacker_id)
        if target is None or attacker is None:
            return

        previous_health = target["health"]
        target["health"] = min(previous_health, reported_health)
        killed = (
            previous_health > 0
            and target["health"] == 0
            and not target["death_recorded"]
        )
        if killed:
            record_profile_result(
                attacker["username"] if attacker_id != target_id else None,
                target["username"],
            )
            target["death_recorded"] = True
            target["deaths"] += 1
            if attacker_id != target_id:
                attacker["kills"] += 1
                attacker["cash"] += KILL_REWARD
            else:
                attacker = None

    broadcast({
        "object": "health_update",
        "id": target_id,
        "health": target["health"],
    })
    if killed:
        if attacker is not None:
            send_to_player(attacker_id, {
                "object": "cash_balance",
                "cash": attacker["cash"],
                "message": f"Kill reward: +${KILL_REWARD}",
            })
            send_to_player(attacker_id, {
                "object": "profile",
                "profile": profile_data(attacker["username"]),
            })
        send_to_player(target_id, {
            "object": "profile",
            "profile": profile_data(target["username"]),
        })
        broadcast_leaderboard()


def process_cash_pickup(identifier, message):
    pickup_id = message.get("id")
    with state_lock:
        pickup = cash_loot.get(pickup_id)
        player = players.get(identifier)
        pickup_is_near = (
            player is not None
            and pickup is not None
            and distance_squared(
                player["position"],
                pickup["position"],
            ) <= CASH_PICKUP_DISTANCE ** 2
        )
        if not pickup_is_near:
            accepted = False
            balance = player["cash"] if player else 0
            amount = 0
        else:
            cash_loot.pop(pickup_id)
            accepted = True
            amount = pickup["amount"]
            player["cash"] += amount
            balance = player["cash"]

    if accepted:
        broadcast({"object": "cash_removed", "id": pickup_id})
    send_to_player(identifier, {
        "object": "cash_pickup_result",
        "id": pickup_id,
        "accepted": accepted,
        "cash": balance,
        "amount": amount,
    })


def process_purchase(identifier, message):
    item_key = message.get("item")
    item = SHOP_CATALOG.get(item_key)
    with state_lock:
        player = players.get(identifier)
        if player is None:
            return

        accepted = item is not None
        message_text = "Purchase complete."
        amount = 0
        ammo_type = None
        health_gain = 0

        machine_distance = distance_squared(
            player["position"],
            VENDING_MACHINE_POSITION,
            axes=(0, 2),
        )
        if machine_distance is None or machine_distance > VENDING_MACHINE_DISTANCE ** 2:
            accepted = False
            message_text = "Move closer to the vending machine."
        elif item is None:
            message_text = "That item is not available."
        elif player["cash"] < item["price"]:
            accepted = False
            message_text = "Not enough cash."
        elif item_key == "health" and (
            player["health"] >= 200 or player["death_recorded"]
        ):
            accepted = False
            message_text = "Health is full or the player is dead."
        else:
            player["cash"] -= item["price"]
            amount = item["amount"]
            ammo_type = item.get("ammo_type")
            if item_key == "health":
                health_gain = min(amount, 200 - player["health"])
                player["health"] += health_gain
                amount = health_gain
                message_text = f"Health restored by {health_gain}."

        result = {
            "object": "purchase_result",
            "accepted": accepted,
            "item": item_key,
            "message": message_text,
            "cash": player["cash"],
            "ammo_type": ammo_type,
            "amount": amount,
            "health": player["health"],
        }
    send_to_player(identifier, result)


def handle_message(identifier, message):
    if not isinstance(message, dict):
        return

    object_type = message.get("object")
    if object_type == "player":
        with state_lock:
            player = players.get(identifier)
            if player is None:
                return
            player["position"] = message.get("position", player["position"])
            player["rotation"] = message.get("rotation", player["rotation"])
            reported_health = max(
                0,
                min(200, int(message.get("health", player["health"]))),
            )
            player["health"] = (
                0 if player["death_recorded"] else reported_health
            )
            player["weapon"] = message.get("weapon", player["weapon"])
            player["shielded"] = bool(message.get("shielded", player["shielded"]))
            message = {**message, "health": player["health"]}
        broadcast(message, exclude=identifier)
    elif object_type == "health_update":
        process_health_update(identifier, message)
    elif object_type in {"bullet", "sniper_bullet", "grenade", "knife", "arrow"}:
        broadcast(message, exclude=identifier)
    elif object_type == "cash_pickup":
        process_cash_pickup(identifier, message)
    elif object_type == "shop_purchase":
        process_purchase(identifier, message)
    elif object_type == "chat":
        with state_lock:
            player = players.get(identifier)
            username = player["username"] if player else "Unknown"
        broadcast({
            "object": "chat",
            "username": username,
            "message": str(message.get("message", ""))[:500],
        })
    elif object_type == "voxel":
        try:
            position = tuple(message["position"])
        except (KeyError, TypeError):
            return
        if message.get("action") == "place":
            with state_lock:
                world_voxels.add(position)
        elif message.get("action") == "destroy":
            with state_lock:
                world_voxels.discard(position)
        else:
            return
        broadcast(message, exclude=identifier)


def handle_messages(identifier):
    with state_lock:
        client_info = players.get(identifier)
    if client_info is None:
        return

    conn = client_info["socket"]
    buffer = bytearray()
    try:
        while True:
            chunk = conn.recv(MSG_SIZE)
            if not chunk:
                break
            buffer.extend(chunk)
            if len(buffer) > 1024 * 1024:
                raise ValueError("Client message buffer exceeded 1 MiB.")
            while b"\n" in buffer:
                raw_message, _, remainder = buffer.partition(b"\n")
                buffer = bytearray(remainder)
                if not raw_message:
                    continue
                try:
                    message = json.loads(raw_message.decode("utf8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    print(f"Invalid message from player {identifier}: {error}")
                    continue
                handle_message(identifier, message)
    except (ConnectionResetError, OSError) as error:
        print(f"Connection to player {identifier} closed: {error}")
    except ValueError as error:
        print(f"Connection to player {identifier} rejected: {error}")
    finally:
        with state_lock:
            player = players.pop(identifier, None)
        if player is not None:
            broadcast({
                "id": identifier,
                "object": "player",
                "username": player["username"],
                "joined": False,
                "left": True,
            }, exclude=identifier)
            broadcast_leaderboard()
            conn.close()
            print(f"Player {player['username']} with ID {identifier} has left.")


def player_message(identifier, player, joined, left=False):
    return {
        "id": identifier,
        "object": "player",
        "username": player["username"],
        "position": player["position"],
        "rotation": player["rotation"],
        "health": player["health"],
        "weapon": player["weapon"],
        "shielded": player["shielded"],
        "joined": joined,
        "left": left,
    }


def main():
    initialize_profiles()
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # add this
    server_socket.bind((ADDR, PORT))
    server_socket.listen(MAX_PLAYERS)
    print(f"Server listening on {ADDR}:{PORT}...")

    threading.Thread(target=ammo_spawner, daemon=True).start()
    threading.Thread(target=medkit_spawner, daemon=True).start()
    threading.Thread(target=cash_spawner, daemon=True).start()

    try:
        while True:
            conn, address = server_socket.accept()
            try:
                with state_lock:
                    identifier = generate_id(players, MAX_PLAYERS)
                if identifier is None:
                    conn.close()
                    continue

                send_json(conn, {"object": "welcome", "id": identifier})
                username = conn.recv(MSG_SIZE).decode("utf8").strip()[:32]
                if not username:
                    username = f"Player {identifier}"
                profile = load_profile(username)

                player = {
                    "socket": conn,
                    "send_lock": threading.Lock(),
                    "username": username,
                    "position": (0, 1, 0),
                    "rotation": 0,
                    "health": 200,
                    "death_recorded": False,
                    "weapon": "medieval-sword/textures/wire_135059008_albedo.jpeg",
                    "shielded": False,
                    "cash": STARTING_CASH,
                    "kills": 0,
                    "deaths": 0,
                    "profile": profile,
                }
                with state_lock:
                    existing_players = [
                        (player_id, current)
                        for player_id, current in players.items()
                    ]
                    players[identifier] = player
                    existing_loot = list(cash_loot.values())
                    existing_voxels = list(world_voxels)

                for player_id, current in existing_players:
                    send_to_player(player_id, player_message(identifier, player, True))
                    send_json(conn, player_message(player_id, current, True), player["send_lock"])

                send_json(conn, {
                    "object": "player_state",
                    "cash": player["cash"],
                    "kills": player["kills"],
                    "deaths": player["deaths"],
                }, player["send_lock"])
                send_json(conn, {
                    "object": "profile",
                    "profile": profile,
                }, player["send_lock"])
                send_json(conn, {
                    "object": "shop_catalog",
                    "items": [
                        {"key": key, **item}
                        for key, item in SHOP_CATALOG.items()
                    ],
                }, player["send_lock"])
                for pickup in existing_loot:
                    send_json(conn, pickup, player["send_lock"])
                for position in existing_voxels:
                    send_json(conn, {
                        "object": "voxel",
                        "action": "place",
                        "position": position,
                    }, player["send_lock"])

                threading.Thread(
                    target=handle_messages,
                    args=(identifier,),
                    daemon=True,
                ).start()
                broadcast_leaderboard()
                print(f"New connection from {address}, assigned ID {identifier}.")
            except (OSError, UnicodeDecodeError) as error:
                print(f"Could not initialize connection from {address}: {error}")
                conn.close()
    except KeyboardInterrupt:
        pass
    finally:
        server_socket.close()


if __name__ == "__main__":
    main()
