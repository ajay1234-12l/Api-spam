# app.py
from flask import Flask, request, jsonify
import json
import requests
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import os
import traceback

# ---------- Safe imports (fail fast with clear message) ----------
try:
    from byte import Encrypt_ID, encrypt_api  # must match your working impl
except Exception as e:
    raise RuntimeError(f"ImportError: 'byte.py' missing or bad. {e}")

try:
    from visit_count_pb2 import Info  # same protobuf class you used in visit API
except Exception as e:
    raise RuntimeError(f"ImportError: 'visit_count_pb2.py' missing or bad. {e}")

app = Flask(__name__)

# ---------- Config ----------
BASE_DIR = Path(__file__).resolve().parent

# Regions: keep lower-case to match token file names token_{region}.json
# Add more regions if you have token files for them.
regions = os.getenv("REGIONS", "ind").split(",")  # e.g. "ind,br,us,na,bd"
regions = [r.strip().lower() for r in regions if r.strip()]

# Concurrency caps (serverless-safe defaults). Override with env if needed.
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "64"))   # 32–128 safe for most serverless
MAX_BURST   = int(os.getenv("MAX_BURST", "256"))    # how many requests per call max

# Request timeouts (seconds)
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "8"))

# ---------- Helpers ----------
def load_tokens():
    """
    Loads tokens from token_{region}.json in the same folder.
    Returns list of tuples: (region_lower, token)
    """
    all_tokens = []
    for region in regions:
        file_path = BASE_DIR / f"token_{region}.json"
        try:
            if not file_path.exists():
                app.logger.warning(f"[WARN] Tokens file not found: {file_path.name}")
                continue
            with file_path.open("r", encoding="utf-8") as f:
                data = json.load(f)

            # tolerate dict list or plain list
            for item in data:
                tok = item.get("token") if isinstance(item, dict) else item
                if tok and tok not in ("", "N/A", None):
                    all_tokens.append((region, str(tok).strip()))
        except Exception as e:
            app.logger.error(f"Error loading tokens from {file_path.name}: {e}")
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
    """
    Parse protobuf Info -> dict
    """
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
        app.logger.error(f"❌ Protobuf parsing error: {e}")
        return None

def fetch_player_info(uid_str: str, region_lower: str, token: str):
    """
    Single attempt to fetch player info via GetPlayerPersonalShow using given token.
    Returns dict or None.
    """
    try:
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

        r = requests.post(url, headers=headers, data=data, timeout=HTTP_TIMEOUT)
        if r.status_code == 200 and r.content:
            return parse_protobuf_response(r.content)
        app.logger.info(f"[INFO] PersonalShow non-200: {r.status_code} body_len={len(r.content)}")
    except Exception as e:
        app.logger.error(f"GetPlayerPersonalShow error ({region_lower}): {e}")
    return None

def request_adding_friend(uid_str: str, region_lower: str, token: str) -> bool:
    """
    Fire one RequestAddingFriend call. Returns True if HTTP 200.
    """
    try:
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

        r = requests.post(url, headers=headers, data=bytes.fromhex(encrypted_payload), timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            app.logger.info(f"[INFO] RequestAddingFriend non-200: {r.status_code}")
        return r.status_code == 200
    except Exception as e:
        app.logger.error(f"Error sending request for region {region_lower}: {e}")
        return False

# ---------- Worker ----------
def spam_worker(uid_str: str,
                region_lower: str,
                token: str,
                results: dict,
                lock: threading.Lock,
                first_info_holder: dict):
    ok = request_adding_friend(uid_str, region_lower, token)
    with lock:
        if ok:
            results["success"] += 1
        else:
            results["failed"] += 1

    # Try to capture player_info once (cheap extra call). Stop after first success.
    if not first_info_holder.get("done", False):
        info = fetch_player_info(uid_str, region_lower, token)
        if info:
            with lock:
                if not first_info_holder.get("done", False):
                    first_info_holder["done"] = True
                    first_info_holder["data"] = info

# ---------- API ----------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True}), 200

@app.route("/send_requests", methods=["GET"])
def send_requests():
    try:
        uid = request.args.get("uid", type=str)
        if not uid:
            return jsonify({"error": "uid parameter is required"}), 400

        # Optional: ensure uid is numeric (if your Encrypt_ID expects digits)
        if not uid.isdigit():
            return jsonify({"error": "uid must be digits"}), 400

        tokens_with_region = load_tokens()
        if not tokens_with_region:
            return jsonify({"error": "No tokens found in any token file"}), 500

        burst = min(MAX_BURST, len(tokens_with_region))

        results = {"success": 0, "failed": 0}
        first_info_holder = {"done": False, "data": None}
        lock = threading.Lock()

        # Thread pool instead of raw 1000 threads (serverless safe)
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = [
                ex.submit(
                    spam_worker, uid, region_lower, token, results, lock, first_info_holder
                )
                for region_lower, token in tokens_with_region[:burst]
            ]
            # Wait for completion
            for _ in as_completed(futures):
                pass

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

    except Exception as e:
        # Return a helpful JSON instead of opaque 500 page
        return jsonify({
            "error": "FUNCTION_INVOCATION_FAILED",
            "detail": str(e),
            "trace": traceback.format_exc()
        }), 500

# ---------- Entrypoint ----------
if __name__ == "__main__":
    # For production, run behind gunicorn/uvicorn with multiple workers
    port = int(os.getenv("PORT", "5000"))
    app.run(debug=True, host="0.0.0.0", port=port)
