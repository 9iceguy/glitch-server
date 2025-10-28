"""
Glitch Multiplayer Server
Handles player connections, synchronization, and messaging.
"""

import socket
import json
import time
import random
import threading
import errno

# Server configuration
ADDR = "0.0.0.0"       # Listen on all interfaces for Render
PORT = 8000            # Render requires open HTTP/TCP port
MAX_PLAYERS = 20
MSG_SIZE = 2048

# Initialize server socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

players = {}


def generate_id(player_list: dict, max_players: int):
    """Generate a unique player ID."""
    while True:
        unique_id = str(random.randint(1, max_players))
        if unique_id not in player_list:
            return unique_id


def handle_messages(identifier: str):
    """Handle incoming player messages and broadcast to others."""
    client_info = players[identifier]
    conn: socket.socket = client_info["socket"]
    username = client_info["username"]

    while True:
        try:
            msg = conn.recv(MSG_SIZE)
        except ConnectionResetError:
            break

        if not msg:
            break

        msg_decoded = msg.decode("utf8")

        try:
            left_bracket_index = msg_decoded.index("{")
            right_bracket_index = msg_decoded.index("}") + 1
            msg_decoded = msg_decoded[left_bracket_index:right_bracket_index]
        except ValueError:
            continue

        try:
            msg_json = json.loads(msg_decoded)
        except Exception as e:
            print("⚠️ JSON parse error:", e)
            continue

        print(f"📩 Received update from {username} (ID {identifier})")

        # Update player info
        if msg_json.get("object") == "player":
            players[identifier]["position"] = msg_json.get("position", (0, 0, 0))
            players[identifier]["rotation"] = msg_json.get("rotation", 0)
            players[identifier]["health"] = msg_json.get("health", 100)

        # Broadcast to others
        for pid, info in players.items():
            if pid != identifier:
                try:
                    info["socket"].sendall(msg_decoded.encode("utf8"))
                except OSError:
                    pass

    # Notify others that player left
    for pid, info in players.items():
        if pid != identifier:
            try:
                info["socket"].send(json.dumps({
                    "id": identifier,
                    "object": "player",
                    "joined": False,
                    "left": True
                }).encode("utf8"))
            except OSError:
                pass

    print(f"👋 Player {username} (ID {identifier}) disconnected.")
    del players[identifier]
    conn.close()


def main():
    """Start the game server."""
    print("🚀 Launching Glitch Multiplayer Server...")

    # Retry binding if port is already in use
    while True:
        try:
            s.bind((ADDR, PORT))
            break
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                print(f"⚠️ Port {PORT} already in use. Retrying in 3s...")
                time.sleep(3)
            else:
                raise e

    s.listen(MAX_PLAYERS)
    print(f"✅ Server started on {ADDR}:{PORT}, waiting for players...")

    try:
        while True:
            try:
                conn, addr = s.accept()
            except KeyboardInterrupt:
                print("🛑 Server manually stopped.")
                break

            # Detect and ignore Render's internal HTTP health probes
            conn.settimeout(2.0)
            try:
                peek_data = conn.recv(64, socket.MSG_PEEK).decode("utf-8", errors="ignore")
                if peek_data.startswith("HEAD /") or peek_data.startswith("GET /"):
                    print(f"🔸 Ignored Render probe from {addr}")
                    conn.close()
                    continue
            except Exception:
                pass

            conn.settimeout(None)

            # Assign unique ID to player
            new_id = generate_id(players, MAX_PLAYERS)
            try:
                conn.send(new_id.encode("utf8"))
            except Exception:
                conn.close()
                continue

            try:
                username = conn.recv(MSG_SIZE).decode("utf8")
            except Exception:
                conn.close()
                continue

            new_player = {
                "socket": conn,
                "username": username,
                "position": (0, 1, 0),
                "rotation": 0,
                "health": 100
            }

            players[new_id] = new_player
            print(f"🎮 New connection from {addr}, ID: {new_id}, user: {username}")

            # Inform others about new player
            for pid, info in players.items():
                if pid != new_id:
                    try:
                        info["socket"].send(json.dumps({
                            "id": new_id,
                            "object": "player",
                            "username": username,
                            "position": new_player["position"],
                            "health": new_player["health"],
                            "joined": True,
                            "left": False
                        }).encode("utf8"))
                    except OSError:
                        pass

            # Inform new player about others
            for pid, info in players.items():
                if pid != new_id:
                    try:
                        conn.send(json.dumps({
                            "id": pid,
                            "object": "player",
                            "username": info["username"],
                            "position": info["position"],
                            "health": info["health"],
                            "joined": True,
                            "left": False
                        }).encode("utf8"))
                        time.sleep(0.1)
                    except OSError:
                        pass

            # Start message thread
            threading.Thread(target=handle_messages, args=(new_id,), daemon=True).start()

    except Exception as e:
        print(f"❌ Server error: {e}")
    finally:
        print("🔻 Closing socket...")
        s.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("🛑 Server stopped manually.")
    finally:
        print("🔻 Exiting cleanly...")
        s.close()
