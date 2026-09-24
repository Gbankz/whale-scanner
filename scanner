"""Whale scanner - stage 1 (Robinhood Chain, chain id 4663).
Given a token address: finds early buyers, top holders, and profiles each wallet.
Reads the API key from the BLOCKSCOUT_API_KEY environment variable (GitHub secret).
"""
import os, sys, json, time, collections
import requests

KEY = os.environ.get("BLOCKSCOUT_API_KEY", "")
BASE = "https://api.blockscout.com/4663/api/v2"
TOKEN = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TOKEN", "")).strip().lower()
MAX_PAGES = int(os.environ.get("MAX_PAGES", "100"))    # transfer pages to read
EARLY_N = int(os.environ.get("EARLY_N", "50"))         # how many first buyers to keep
PROFILE_N = int(os.environ.get("PROFILE_N", "15"))     # wallets to deep-profile
POOL = os.environ.get("POOL", "").strip().lower()      # optional: force pool address
ZERO = "0x0000000000000000000000000000000000000000"

def get(path, params=None):
    p = dict(params or {})
    p["apikey"] = KEY
    for attempt in range(5):
        try:
            r = requests.get(BASE + path, params=p, timeout=30)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1)); continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == 4:
                print(f"! request failed {path}: {e}", file=sys.stderr)
                return None
            time.sleep(1.5)

def addr(x):
    if isinstance(x, dict):
        return (x.get("hash") or x.get("address_hash") or x.get("address") or "").lower()
    return (x or "").lower()

def fetch_all(path, max_pages):
    items, params = [], {}
    for _ in range(max_pages):
        d = get(path, params)
        if not d: break
        items += d.get("items", [])
        nxt = d.get("next_page_params")
        if not nxt: break
        params = nxt
    return items

def main():
    if not KEY or not TOKEN:
        sys.exit("Need BLOCKSCOUT_API_KEY secret and a token address.")
    info = get(f"/tokens/{TOKEN}") or {}
    symbol = info.get("symbol") or "?"
    dec = int(info.get("decimals") or 18)
    print(f"Token: {info.get('name')} ({symbol}), holders: {info.get('holders_count') or info.get('holders')}")

    tr = fetch_all(f"/tokens/{TOKEN}/transfers", MAX_PAGES)
    tr.reverse()  # API is newest-first; we want oldest-first
    print(f"Loaded {len(tr)} transfers")
    if not tr:
        sys.exit("No transfers found - check the address.")

    def amount(t):
        try: return int((t.get("total") or {}).get("value") or 0) / 10 ** dec
        except Exception: return 0.0

    # Guess the pool: the busiest counterparty (skip mint address)
    cnt = collections.Counter()
    for t in tr:
        for a in (addr(t.get("from")), addr(t.get("to"))):
            if a and a != ZERO: cnt[a] += 1
    pool = POOL or (cnt.most_common(1)[0][0] if cnt else "")
    print(f"Pool guess: {pool}")

    w = {}
    def wal(a):
        return w.setdefault(a, dict(wallet=a, buys=0, sells=0, bought=0.0, sold=0.0,
                                    first_buy_block=None, first_buy_time=None, buy_rank=None))
    order = 0
    for t in tr:
        f, to, amt = addr(t.get("from")), addr(t.get("to")), amount(t)
        if f == pool and to not in (pool, ZERO):
            s = wal(to); s["buys"] += 1; s["bought"] += amt
            if s["first_buy_block"] is None:
                order += 1
                s.update(first_buy_block=t.get("block_number"), first_buy_time=t.get("timestamp"), buy_rank=order)
        elif to == pool and f not in (pool, ZERO):
            s = wal(f); s["sells"] += 1; s["sold"] += amt

    for s in w.values():
        s["sold_pct"] = round(100 * s["sold"] / s["bought"], 1) if s["bought"] else None

    early = sorted([s for s in w.values() if s["buy_rank"]], key=lambda s: s["buy_rank"])[:EARLY_N]

    holders = fetch_all(f"/tokens/{TOKEN}/holders", 3)
    top = []
    for h in holders[:25]:
        a = addr(h.get("address"))
        try: bal = int(h.get("value") or 0) / 10 ** dec
        except Exception: bal = 0
        top.append(dict(wallet=a, balance=bal, early_rank=(w.get(a) or {}).get("buy_rank"),
                        sold_pct=(w.get(a) or {}).get("sold_pct")))
    print("Top holders loaded:", len(top))

    # Deep profile: early buyers still relevant + top holders
    targets = []
    for a in [e["wallet"] for e in early] + [t["wallet"] for t in top]:
        if a and a != pool and a not in targets: targets.append(a)
    profiles = []
    for a in targets[:PROFILE_N]:
        ai = get(f"/addresses/{a}") or {}
        tb = get(f"/addresses/{a}/token-balances") or []
        if isinstance(tb, dict): tb = tb.get("items", [])
        others = []
        for b in tb:
            tk = b.get("token") or {}
            ta = addr(tk) or (tk.get("address_hash") or "").lower()
            if ta == TOKEN: continue
            others.append(dict(symbol=tk.get("symbol"), name=tk.get("name"), token=ta, raw=b.get("value")))
        try: eth = int(ai.get("coin_balance") or 0) / 1e18
        except Exception: eth = None
        profiles.append(dict(wallet=a, eth_balance=eth, other_tokens=others[:30],
                             other_token_count=len(others), stats=w.get(a)))
        time.sleep(0.3)

    out = dict(token=TOKEN, symbol=symbol, pool_guess=pool, transfers_read=len(tr),
               early_buyers=early, top_holders=top, profiles=profiles,
               generated=time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()))
    os.makedirs("docs", exist_ok=True)
    with open(f"docs/{symbol}_{TOKEN[:8]}.json", "w") as fh: json.dump(out, fh, indent=2, default=str)
    with open("docs/latest.json", "w") as fh: json.dump(out, fh, indent=2, default=str)

    print("\n=== EARLY BUYERS (first 15) ===")
    for e in early[:15]:
        print(f"#{e['buy_rank']:>3} {e['wallet']} bought {e['bought']:.0f} sold {e['sold_pct']}% block {e['first_buy_block']}")
    print("\n=== TOP HOLDERS (first 10) ===")
    for t in top[:10]:
        print(f"{t['wallet']} bal {t['balance']:.0f} early_rank {t['early_rank']}")

if __name__ == "__main__":
    main()
