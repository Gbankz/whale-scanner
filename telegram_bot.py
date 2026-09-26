"""Telegram check-bot: paste a contract address in Telegram, get the pre-buy checklist back.
Runs on a schedule via GitHub Actions (no server needed) - polls for new messages, replies, saves state.
"""
import os, sys, json, re, time, requests

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")          # only this chat gets replies
BLOCKSCOUT_KEY = os.environ.get("BLOCKSCOUT_API_KEY", "")
ETHERSCAN_KEY = os.environ.get("ETHERSCAN_API_KEY", "")   # free key from etherscan.io works across chains (V2 API)
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")              # auto-provided by Actions, used only to trigger the deep-scan workflow
GH_REPO = os.environ.get("GITHUB_REPOSITORY", "")          # e.g. "yourname/whale-scanner", auto-provided by Actions
STATE_FILE = "docs/telegram_state.json"
API = f"https://api.telegram.org/bot{TOKEN}"

CHAINS = {
    "robinhood": dict(kind="blockscout", base="https://api.blockscout.com/4663/api/v2", label="Robinhood Chain"),
    "eth":       dict(kind="etherscan", id=1, label="Ethereum"),
    "bsc":       dict(kind="etherscan", id=56, label="BNB Chain"),
    "base":      dict(kind="etherscan", id=8453, label="Base"),
    "polygon":   dict(kind="etherscan", id=137, label="Polygon"),
    "arbitrum":  dict(kind="etherscan", id=42161, label="Arbitrum"),
}
DEFAULT_CHAIN = "robinhood"
ADDR_RE = re.compile(r"0x[0-9a-fA-F]{40}")

def load_state():
    try: return json.load(open(STATE_FILE))
    except Exception: return {"offset": 0}

def save_state(s):
    os.makedirs("docs", exist_ok=True)
    json.dump(s, open(STATE_FILE, "w"))

def tg(method, **params):
    r = requests.post(f"{API}/{method}", json=params, timeout=20)
    r.raise_for_status()
    return r.json().get("result")

def send(chat_id, text):
    for i in range(0, len(text), 3800):  # telegram message length limit
        tg("sendMessage", chat_id=chat_id, text=text[i:i+3800], disable_web_page_preview=True)

# ---- Blockscout (Robinhood Chain) ----
def bs_get(base, path, params=None):
    p = dict(params or {}); p["apikey"] = BLOCKSCOUT_KEY
    r = requests.get(base + path, params=p, timeout=20)
    return r.json() if r.ok else None

def check_blockscout(base, addr):
    lines, risk = [], 0
    info = bs_get(base, f"/tokens/{addr}") or {}
    if not info: return None
    lines.append(f"{info.get('name','?')} ({info.get('symbol','?')})")
    sc = requests.get(f"{base}/smart-contracts/{addr}").ok if True else False
    verified = requests.get(f"{base}/smart-contracts/{addr}").status_code == 200
    lines.append(f"{'✓' if verified else '! not'} verified" if verified else "! not verified — can't inspect code"); risk += 0 if verified else 1
    ai = bs_get(base, f"/addresses/{addr}") or {}
    creator = ai.get("creator_address_hash")
    if ai.get("creation_tx_hash"):
        tx = bs_get(base, f"/transactions/{ai['creation_tx_hash']}") or {}
        ts = tx.get("timestamp")
        if ts:
            age_h = (time.time() - time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))) / 3600
            lines.append(f"{'!' if age_h<24 else '✓'} age: {age_h:.0f}h old" + (" — very new" if age_h<24 else ""))
            risk += 1 if age_h < 24 else 0
    if creator:
        dtx = bs_get(base, f"/addresses/{creator}/transactions") or {}
        created = sum(1 for t in (dtx.get("items") or []) if t.get("created_contract"))
        lines.append(f"deployer {creator[:8]}… has {created} other contract(s)")
        risk += 1 if created > 1 else 0
    holders = bs_get(base, f"/tokens/{addr}/holders") or {}
    items = holders.get("items") or []
    try:
        dec = int(info.get("decimals") or 18); supply = float(info.get("total_supply") or 0) / 10**dec
        top10 = sum(float(h.get("value") or 0) / 10**dec for h in items[1:11])
        pct = round(100 * top10 / supply) if supply else None
        if pct is not None:
            lines.append(f"{'✗' if pct>40 else '!' if pct>20 else '✓'} top 10 holders (excl. pool): {pct}%")
            risk += 2 if pct > 40 else (1 if pct > 20 else 0)
    except Exception: pass
    verdict = "🔴 HIGH RISK" if risk >= 3 else "🟡 CAUTION" if risk >= 1 else "🟢 no automatic red flags"
    lines.append("(not checked: liquidity lock, buy/sell tax — verify manually)")
    return verdict + "\n" + "\n".join(lines)

# ---- Etherscan V2 unified API (other EVM chains) ----
def es_get(chain_id, **params):
    params.update(chainid=chain_id, apikey=ETHERSCAN_KEY)
    r = requests.get("https://api.etherscan.io/v2/api", params=params, timeout=20)
    return r.json() if r.ok else None

def check_etherscan(chain_id, addr, label):
    if not ETHERSCAN_KEY:
        return "Need an ETHERSCAN_API_KEY secret to check chains other than Robinhood Chain (free at etherscan.io/apis)."
    lines, risk = [f"{label} contract"], 0
    src = es_get(chain_id, module="contract", action="getsourcecode", address=addr)
    result = (src or {}).get("result") or [{}]
    verified = bool(result[0].get("SourceCode"))
    lines.append(f"{'✓' if verified else '!'} {'verified' if verified else 'not verified — cannot inspect code'}"); risk += 0 if verified else 1
    cc = es_get(chain_id, module="contract", action="getcontractcreation", contractaddresses=addr)
    ccr = ((cc or {}).get("result") or [{}])[0]
    creator, tx_hash = ccr.get("contractCreator"), ccr.get("txHash")
    if tx_hash:
        txr = es_get(chain_id, module="proxy", action="eth_getTransactionByHash", txhash=tx_hash) or {}
        blk = (txr.get("result") or {}).get("blockNumber")
        if blk:
            b = es_get(chain_id, module="block", action="getblockreward", blockno=int(blk, 16)) or {}
            ts = (b.get("result") or {}).get("timeStamp")
            if ts:
                age_h = (time.time() - int(ts)) / 3600
                lines.append(f"{'!' if age_h<24 else '✓'} age: {age_h:.0f}h old" + (" — very new" if age_h<24 else ""))
                risk += 1 if age_h < 24 else 0
    if creator:
        txl = es_get(chain_id, module="account", action="txlist", address=creator, sort="desc", offset=200) or {}
        created = sum(1 for t in (txl.get("result") or []) if t.get("contractAddress"))
        lines.append(f"deployer {creator[:8]}… has {created} other contract(s) in recent history")
        risk += 1 if created > 1 else 0
    lines.append("(holder concentration and liquidity lock need a paid data source on this chain — not checked)")
    verdict = "🔴 HIGH RISK" if risk >= 2 else "🟡 CAUTION" if risk >= 1 else "🟢 no automatic red flags"
    return verdict + "\n" + "\n".join(lines)

def trigger_deep_scan(addr, watchlist=""):
    if not (GH_TOKEN and GH_REPO):
        return "Deep scan isn't set up: this bot's Actions run needs 'actions: write' permission."
    r = requests.post(
        f"https://api.github.com/repos/{GH_REPO}/actions/workflows/scan.yml/dispatches",
        headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"},
        json={"ref": "main", "inputs": {"token": addr, "watchlist": watchlist}}, timeout=20)
    if r.status_code == 204:
        return f"Deep scan started for {addr[:10]}... Results + a summary will land here in a few minutes."
    return f"Couldn't start the scan (status {r.status_code}): {r.text[:200]}"

def handle(text):
    text = text.strip()
    m = ADDR_RE.search(text)
    if not m:
        return ("Send a contract address for a quick check, optionally with a chain name first "
                "(robinhood default, or: eth, bsc, base, polygon, arbitrum). Example: eth 0xabc...\n"
                "Send 'scan <address>' for the full whale/funder deep scan (Robinhood Chain only).")
    addr = m.group(0).lower()
    first_word = text.split()[0].lower()
    if first_word in ("scan", "deep", "/scan"):
        return trigger_deep_scan(addr)
    chain_word = first_word if not text.lower().startswith("0x") else DEFAULT_CHAIN
    chain = CHAINS.get(chain_word, CHAINS[DEFAULT_CHAIN])
    try:
        if chain["kind"] == "blockscout":
            out = check_blockscout(chain["base"], addr)
            return out or "No token found at that address on Robinhood Chain."
        else:
            return check_etherscan(chain["id"], addr, chain["label"])
    except Exception as e:
        return f"Check failed: {e}"

def main():
    if not TOKEN:
        sys.exit("Missing TELEGRAM_BOT_TOKEN secret.")
    state = load_state()
    updates = tg("getUpdates", offset=state.get("offset", 0), timeout=0) or []
    for u in updates:
        state["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        text = msg.get("text", "")
        if not text: continue
        if CHAT_ID and chat_id != CHAT_ID:
            continue  # ignore anyone but the configured chat
        if not CHAT_ID:
            send(chat_id, f"Your chat id is {chat_id} — add it as the TELEGRAM_CHAT_ID secret so only you get replies.")
            continue
        send(chat_id, handle(text))
    save_state(state)
    print(f"Processed {len(updates)} update(s).")

if __name__ == "__main__":
    main()
