from flask import Flask, request, jsonify
import requests
import json
import threading
from byte import Encrypt_ID, encrypt_api  # must match your working impl
from visit_count_pb2 import Info          # same protobuf class you used in visit api

app = Flask(__name__)

# ---------------- Config ----------------
# Keep regions lower-case in the list (files are token_ind.json, token_br.json, etc.)
regions = ["ind"]  # add more like "br", "us", "sac", "na", "bd" as you have token files

# ---------------- Helpers ----------------
def load_tokens():
    """
    Loads tokens from token_{region}.json and returns list of tuples: (region_lower, token)
    """
    all_tokens = []
    for region in regions:
        file_name = f"token_{region}.json"
        try:
            with open(file_name, "r") as f:
                data = json.load(f)
            # tolerate dict list or plain list
            for item in data:
                tok = item.get("token") if isinstance(item, dict) else item
                if tok and tok not in ("", "N/A", None):
                    all_tokens.append((region, tok.strip()))
        except Exception as e:
            print(f"Error loading tokens from {file_name}: {e}")
    return all_tokens

def personal_show_base(region_upper: str) -> str:
    """
    Map region to GetPlayerPersonalShow base host (same logic as visit API).
    """
    if region_upper == "IND":
        return "client.ind.freefiremobile.com"
    elif region_upper in {"BR", "US", "SAC", "NA"}:
        return "client.us.freefiremobile.com"
    else:
        # fallback for BD/others (same as your visit API)
        return "clientbp.ggblueshark.com"

def parse_protobuf_response(raw_bytes):
    try:
        info = Info()
        info.ParseFromString(raw_bytes)
        return {
            "uid": info.AccountInfo.UID if info.AccountInfo.UID else 0,
            "nickname": info.AccountInfo.PlayerNickname if info.AccountInfo.PlayerNickname else "",
            "likes": info.AccountInfo.Likes if info.AccountInfo.Likes else 0,
            "region": info.AccountInfo.PlayerRegion if info.AccountInfo.PlayerRegion else "",
            "level": info.AccountInfo.Levels if info.AccountInfo.Levels else 0,
        }
    except Exception as e:
        print(f"❌ Protobuf parsing error: {e}")
        return None

def fetch_player_info(uid_str: str, region_lower: str, token: str):
    """
    Single attempt to fetch player info via GetPlayerPersonalShow using given token.
    Returns dict or None.
    """
    region_upper = region_lower.upper()
    host = personal_show_base(region_upper)
    url = f"https://{host}/GetPlayerPersonalShow"

    # same payload pattern as visit API: "08" + Encrypt_ID(uid) + "1801"
    payload_hex = encrypt_api("08" + Encrypt_ID(uid_str) + "1801")
    data = bytes.fromhex(payload_hex)

    headers = {
        "ReleaseVersion": "OB51",
        "X-GA": "v1 1",
        "Authorization": f"Bearer {token}",
        "Host": host,
        "Content-Type": "application/x-www-form-urlencoded"
    }

    try:
        resp = requests.post(url, headers=headers, data=data, timeout=10, verify=False)
        if resp.status_code == 200 and resp.content:
            return parse_protobuf_response(resp.content)
    except Exception as e:
        print(f"GetPlayerPersonalShow error ({region_lower}): {e}")
    return None

def request_adding_friend(uid_str: str, region_lower: str, token: str) -> bool:
    """
    Fire one RequestAddingFriend call. Returns True if HTTP 200.
    """
    encrypted_id = Encrypt_ID(uid_str)
    payload = f"08a7c4839f1e10{encrypted_id}1801"
    encrypted_payload = encrypt_api(payload)

    url = f"https://client.{region_lower}.freefiremobile.com/RequestAddingFriend"
    headers = {
        "Expect": "100-continue",
        "Authorization": f"Bearer {token}",
        "X-Unity-Version": "2018.4.11f1",
        "X-GA": "v1 1",
        "ReleaseVersion": "OB51",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9; SM-N975F Build/PI)",
        "Host": f"client.{region_lower}.freefiremobile.com",
        "Connection": "close",
        "Accept-Encoding": "gzip, deflate, br"
    }

    try:
        r = requests.post(url, headers=headers, data=bytes.fromhex(encrypted_payload), timeout=10, verify=False)
        return r.status_code == 200
    except Exception as e:
        print(f"Error sending request for region {region_lower}: {e}")
        return False

# ---------------- Worker ----------------
def spam_worker(uid_str: str, region_lower: str, token: str, results: dict, lock: threading.Lock, first_info_holder: dict):
    ok = request_adding_friend(uid_str, region_lower, token)
    with lock:
        if ok:
            results["success"] += 1
        else:
            results["failed"] += 1

    # Try to capture player_info once (cheap extra call). Stop after first success.
    # We only let a few threads attempt this to avoid stampede.
    if not first_info_holder.get("done", False):
        info = fetch_player_info(uid_str, region_lower, token)
        if info:
            with lock:
                if not first_info_holder.get("done", False):
                    first_info_holder["done"] = True
                    first_info_holder["data"] = info

# ---------------- API ----------------
@app.route("/send_requests", methods=["GET"])
def send_requests():
    uid = request.args.get("uid", type=str)

    if not uid:
        return jsonify({"error": "uid parameter is required"}), 400

    tokens_with_region = load_tokens()
    if not tokens_with_region:
        return jsonify({"error": "No tokens found in any token file"}), 500

    # Limit how many total requests to send (change as you like)
    max_burst = min(1000, len(tokens_with_region))

    results = {"success": 0, "failed": 0}
    first_info_holder = {"done": False, "data": None}
    lock = threading.Lock()
    threads = []

    for region_lower, token in tokens_with_region[:max_burst]:
        t = threading.Thread(target=spam_worker, args=(uid, region_lower, token, results, lock, first_info_holder))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    status = 1 if results["success"] > 0 else 2

    resp = {
        "uid": uid,
        "success_count": results["success"],
        "failed_count": results["failed"],
        "status": status
    }

    # Attach player info if captured
    if first_info_holder.get("data"):
        info = first_info_holder["data"]
        resp.update({
            "level": info.get("level", 0),
            "likes": info.get("likes", 0),
            "nickname": info.get("nickname", ""),
            "region": info.get("region", ""),
        })

    return jsonify(resp), 200

# ---------------- Entrypoint ----------------
if __name__ == "__main__":
    # For production, run behind gunicorn/uvicorn with multiple workers
    app.run(debug=True, host="0.0.0.0", port=5000)
