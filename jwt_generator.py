import json
import time
import asyncio
import httpx
import subprocess
import os
import requests
from typing import Dict, Optional

# --- Settings ---
RELEASEVERSION = "OB50"
USERAGENT = "Dalvik/2.1.0 (Linux; U; Android 13; CPH2095 Build/RKQ1.211119.001)"
TELEGRAM_TOKEN = "8379507521:AAEhNHj6l1hHSNvn84stlTpvqBIsRSgAhjs"
TELEGRAM_CHAT_ID = 7968668273
BRANCH_NAME = "main"
JWT_API_URL = "https://jwt-api-aditya-ffm.vercel.app/token"

# --- Telegram ---
def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, data=data)
    except:
        pass

# --- Git Helpers ---
def run_git_command(cmd):
    try:
        result = subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT, universal_newlines=True)
        return result.strip()
    except subprocess.CalledProcessError as e:
        return e.output.strip()

def detect_git_conflict():
    status = run_git_command("git status")
    return "You are not currently on a branch" in status or "both modified" in status or "Unmerged paths" in status

def resolve_git_conflict():
    print("\n⚠️ Git Conflict Detected. Please manually resolve conflicts and save files.")
    input("➡️ Press Enter once conflicts are resolved and files are saved...")
    run_git_command("git add .")
    run_git_command("git rebase --continue")
    print("✅ Rebase continued.")

def push_to_git():
    run_git_command(f"git checkout {BRANCH_NAME}")
    run_git_command(f"git push origin {BRANCH_NAME}")
    print(f"🚀 Changes pushed to {BRANCH_NAME} branch.")

def get_repo_and_filename(region):
    """Determine repository and filename based on region"""
    if region == "IND":
        return "token_ind.json"
    elif region in {"BR", "US", "SAC", "NA"}:
        return "token_br.json"
    else:
        return "token_bd.json"

# --- Token Generation ---
async def generate_jwt_token(client, uid: str, password: str) -> Optional[Dict]:
    """Generate JWT token using the API endpoint"""
    try:
        url = f"{JWT_API_URL}?uid={uid}&password={password}"
        headers = {
            'User-Agent': USERAGENT,
            'Accept': 'application/json',
        }
        
        resp = await client.get(url, headers=headers, timeout=30)
        
        if resp.status_code == 200:
            return resp.json()
        return None
    except Exception as e:
        print(f"Error generating token for {uid}: {str(e)}")
        return None

async def process_account_with_retry(client, index, uid, password, max_retries=2):
    for attempt in range(max_retries):
        try:
            token_data = await generate_jwt_token(client, uid, password)
            if token_data and "token" in token_data:
                return {
                    "serial": index + 1,
                    "uid": uid,
                    "password": password,
                    "token": token_data["token"],
                    "notiRegion": token_data.get("notiRegion", "")
                }
        except Exception as e:
            print(f"Attempt {attempt + 1} failed for UID #{index + 1}: {str(e)}")

        if attempt < max_retries - 1:  
            print(f"⏳ UID #{index + 1} {uid} - Retry after 1 minute...")  
            await asyncio.sleep(60)  

    return {  
        "serial": index + 1,  
        "uid": uid,  
        "password": password,  
        "token": None,  
        "notiRegion": ""  
    }

async def generate_tokens_for_region(region):
    start_time = time.time()

    input_file = f"uid_{region}.json"  
    if not os.path.exists(input_file):  
        print(f"⚠️ {input_file} not found.")  
        return  

    with open(input_file, "r") as f:  
        accounts = json.load(f)  

    total_accounts = len(accounts)  
    print(f"🚀 Starting Token Generation for {region} Region using API...\n")  

    region_tokens = []  
    failed_serials = []  
    failed_values = []  

    async with httpx.AsyncClient() as client:  
        tasks = []
        for index, account in enumerate(accounts):
            tasks.append(process_account_with_retry(client, index, account["uid"], account["password"]))
        
        results = await asyncio.gather(*tasks)
        
        for result in results:
            serial = result["serial"]
            uid = result["uid"]
            token = result["token"]
            token_region = result.get("notiRegion", "")

            if token and token_region == region:
                region_tokens.append({"uid": uid, "token": token})
                print(f"✅ UID #{serial} {uid} - Token saved for {region}")
            else:
                failed_serials.append(serial)
                failed_values.append(uid)
                print(f"❌ UID #{serial} {uid} - Token generation failed for {region}")

    output_file = get_repo_and_filename(region)
    with open(output_file, "w") as f:  
        json.dump(region_tokens, f, indent=2)  

    total_time = time.time() - start_time
    minutes = int(total_time // 60)
    seconds = int(total_time % 60)

    summary = (  
        f"✅ *{region} Token Generation Complete*\n\n"  
        f"🔹 *Total Tokens:* {len(region_tokens)}\n"  
        f"🔢 *Total Accounts:* {total_accounts}\n"  
        f"❌ *Failed UIDs:* {len(failed_serials)}\n"  
        f"🔸 *Failed UID Serials:* {', '.join(map(str, failed_serials)) or 'None'}\n"  
        f"🔸 *Failed UID Values:* {', '.join(map(str, failed_values)) or 'None'}\n"  
        f"⏱️ *Time Taken:* {minutes} minutes {seconds} seconds\n"  
    )  
    send_telegram_message(summary)  
    print(summary)
    
    return len(region_tokens)

# --- Run ---
if __name__ == "__main__":
    regions = ["IND", "BD", "NA"]
    total_tokens = 0
    
    for region in regions:
        send_telegram_message(f"🤖 Dear Aditya,\n{region} Token Generation Started...⚙️")
        tokens_generated = asyncio.run(generate_tokens_for_region(region))
        total_tokens += tokens_generated

    send_telegram_message(f"🤖 All Regions Completed!\nTotal Tokens Generated: {total_tokens}")

    if detect_git_conflict():  
        print("\n⚠️ Git conflict detected during previous rebase.")  
        resolve_git_conflict()  

    print("🚀 Pushing changes to Git...")  
    push_to_git()