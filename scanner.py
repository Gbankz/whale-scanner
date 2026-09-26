"""Whale scanner - stage 1 (Robinhood Chain, chain id 4663).
Given a token address: finds early buyers, top holders, and profiles each wallet.
Reads the API key from the BLOCKSCOUT_API_KEY environment variable (GitHub secret).
"""
import os, sys, json, time, collections
import requests

KEY = os.environ.get("BLOCKSCOUT_API_KEY", "")
BASE = "https://api.blockscout.com/4663/api/v2"
TOKEN = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TOKEN", "")).strip().lower()
MAX_PAGES = int(os.environ.get("MAX_PAGES", "3000"))    # transfer pages to read
EARLY_N = int(os.environ.get("EARLY_N", "50"))         # how many first buyers to keep
PROFILE_N = int(os.environ.get("PROFILE_N", "15"))     # wallets to deep-profile
POOL = os.environ.get("POOL", "").strip().lower()      # optional: force pool address
WATCHLIST = {}
for entry in os.environ.get("WATCHLIST", "").split(","):
    entry = entry.strip()
    if not entry: continue
    parts = entry.split("|")
    WATCHLIST[parts[0].strip().lower()] = parts[1].strip() if len(parts) > 1 else parts[0][:10]
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

TRUNCATED = {}
def fetch_all(path, max_pages):
    items, params = [], {}
    for i in range(max_pages):
        d = get(path, params)
        if not d: break
        items += d.get("items", [])
        nxt = d.get("next_page_params")
        if not nxt: break
        params = nxt
        if i == max_pages - 1: TRUNCATED[path] = True
        if i and i % 100 == 0: print(f"  ...{len(items)} items read", flush=True)
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
    def near(a, b): return a > 0 and abs(a - b) <= 0.05 * a

    # group transfers by transaction so router hops (pool -> router -> wallet) resolve to the real wallet
    groups, order_keys = {}, []
    for t in tr:
        k = t.get("transaction_hash") or t.get("tx_hash") or id(t)
        if k not in groups: groups[k] = []; order_keys.append(k)
        groups[k].append(t)

    order = 0
    for k in order_keys:
        g = groups[k]
        for t in g:
            f, to, amt = addr(t.get("from")), addr(t.get("to")), amount(t)
            if f == pool and to not in (pool, ZERO):        # BUY: follow forwards to the end wallet
                who = to
                for _ in range(3):
                    nxt = [x for x in g if addr(x.get("from")) == who and near(amt, amount(x)) and addr(x.get("to")) not in (pool, ZERO)]
                    if not nxt: break
                    who = addr(nxt[0].get("to"))
                if who in (pool, ZERO): continue
                s = wal(who); s["buys"] += 1; s["bought"] += amt
                if s["first_buy_block"] is None:
                    order += 1
                    s.update(first_buy_block=t.get("block_number"), first_buy_time=t.get("timestamp"), buy_rank=order)
            elif to == pool and f not in (pool, ZERO):     # SELL: trace back to the wallet that started it
                who = f
                for _ in range(3):
                    prv = [x for x in g if addr(x.get("to")) == who and near(amt, amount(x)) and addr(x.get("from")) not in (pool, ZERO)]
                    if not prv: break
                    who = addr(prv[0].get("from"))
                s = wal(who); s["sells"] += 1; s["sold"] += amt

    for s in w.values():
        s["sold_pct"] = round(100 * s["sold"] / s["bought"], 1) if s["bought"] else None

    early = sorted([s for s in w.values() if s["buy_rank"]], key=lambda s: s["buy_rank"])[:EARLY_N]

    holders = fetch_all(f"/tokens/{TOKEN}/holders", 6)
    top = []
    for h in holders[:100]:
        a = addr(h.get("address"))
        try: bal = int(h.get("value") or 0) / 10 ** dec
        except Exception: bal = 0
        top.append(dict(wallet=a, balance=bal, early_rank=(w.get(a) or {}).get("buy_rank"),
                        sold_pct=(w.get(a) or {}).get("sold_pct")))
    print("Top holders loaded:", len(top))
    # flag groups of holders with near-identical balances (possible airdrop / linked wallets)
    clusters, used = [], set()
    for i, a in enumerate(top):
        if i in used or a["balance"] <= 0: continue
        grp = [j for j, b in enumerate(top) if j not in used and abs(b["balance"] - a["balance"]) <= 0.03 * a["balance"]]
        if len(grp) >= 4:
            used.update(grp)
            clusters.append(dict(approx_balance=round(a["balance"]), wallets=[top[j]["wallet"] for j in grp]))

    # Deep profile: early buyers still relevant + top holders
    targets = []
    for a in [e["wallet"] for e in early] + [t["wallet"] for t in top]:
        if a and a != pool and a not in targets: targets.append(a)
    profiles = []
    for a in targets[:PROFILE_N]:
        ai = get(f"/addresses/{a}") or {}
        tb = get(f"/addresses/{a}/token-balances") or []
        if isinstance(tb, dict): tb = tb.get("items", [])
        others, dust, hits = [], 0, []
        for b in tb:
            tk = b.get("token") or {}
            ta = addr(tk) or (tk.get("address_hash") or "").lower()
            if ta == TOKEN: continue
            try: bal = int(b.get("value") or 0) / 10 ** int(tk.get("decimals") or 18)
            except Exception: bal = 0
            price = tk.get("exchange_rate")
            usd = (bal * float(price)) if price not in (None, "") else None
            # dust heuristic: has a known price and it's worth under $1, or supply is absurdly large (spam token pattern)
            supply_ok = True
            try:
                if tk.get("total_supply") and int(tk["total_supply"]) / 10 ** int(tk.get("decimals") or 18) > 1e15:
                    supply_ok = False
            except Exception: pass
            is_dust = (usd is not None and usd < 1) or not supply_ok
            entry = dict(symbol=tk.get("symbol"), name=tk.get("name"), token=ta, balance=bal, usd=usd)
            if is_dust: dust += 1; continue
            others.append(entry)
            if ta in WATCHLIST: hits.append(dict(token=ta, symbol=tk.get("symbol") or WATCHLIST[ta]))
        others.sort(key=lambda o: (o["usd"] is None, -(o["usd"] or 0)))
        try: eth = int(ai.get("coin_balance") or 0) / 1e18
        except Exception: eth = None
        profiles.append(dict(wallet=a, eth_balance=eth, other_tokens=others[:30], other_token_count=len(others),
                             dust_token_count=dust, watchlist_hits=hits, stats=w.get(a)))
        time.sleep(0.3)

    # ---- link tracing: who funded each wallet, and who moved tokens between them ----
    FUNDER_N = int(os.environ.get("FUNDER_N", "60"))
    cl_wallets = {c for cl in clusters for c in cl["wallets"]}
    watch = [a for a in dict.fromkeys([e["wallet"] for e in early] + [t["wallet"] for t in top] + list(cl_wallets)) if a and a != pool]
    def blk(t): return int(t.get("block_number") or t.get("block") or 0)
    def funder_of(a):
        d = get(f"/addresses/{a}/transactions", {"sort": "block_number", "order": "asc"}) or {}
        items = d.get("items", [])
        if len(items) > 1 and blk(items[0]) > blk(items[-1]):   # sort ignored -> page manually
            items, p = [], {}
            for _ in range(8):
                d = get(f"/addresses/{a}/transactions", p) or {}
                items += d.get("items", [])
                p = d.get("next_page_params")
                if not p: break
            items = items[::-1] if not p else []               # only trust a full history
        for t in items:
            try:
                if addr(t.get("to")) == a and int(t.get("value") or 0) > 0: return addr(t.get("from"))
            except Exception: pass
        return None
    funders = {}
    for a in watch[:FUNDER_N]:
        f = funder_of(a)
        if f: funders[a] = f
        time.sleep(0.25)
    by_f = collections.defaultdict(list)
    for a, f in funders.items(): by_f[f].append(a)
    shared = {f: v for f, v in by_f.items() if len(v) >= 2}
    wset = set(watch)
    edges = collections.Counter()
    for t in tr:
        f, to = addr(t.get("from")), addr(t.get("to"))
        if f in wset and to in wset and f != to: edges[(f, to)] += amount(t)
    transfer_edges = [dict(**{"from": f}, to=to, amount=v) for (f, to), v in edges.most_common(300)]
    launch = int(early[0]["first_buy_block"] or 0) if early else 0
    flags = {}
    for e in early:
        fl = flags.setdefault(e["wallet"], [])
        if launch and int(e["first_buy_block"] or 0) - launch <= 50: fl.append("sniper (bought within ~5s of launch)")
    for f, v in shared.items():
        for a in v: flags.setdefault(a, []).append("shared funder")
    for a in cl_wallets: flags.setdefault(a, []).append("same-balance cluster")
    for p in profiles:
        for h in p.get("watchlist_hits", []):
            flags.setdefault(p["wallet"], []).append(f"holds watchlist token {h['symbol']}")
    print(f"Funders found: {len(funders)}, shared funders: {len(shared)}, wallet-to-wallet transfers: {len(transfer_edges)}")
    if WATCHLIST: print(f"Watchlist tokens configured: {len(WATCHLIST)}")

    out = dict(token=TOKEN, symbol=symbol, watchlist=WATCHLIST, pool_guess=pool, transfers_read=len(tr),
               truncated=TRUNCATED, first_transfer_block=tr[0].get("block_number"), first_transfer_from=addr(tr[0].get("from")),
               balance_clusters=clusters, funders=funders, shared_funders=shared, transfer_edges=transfer_edges, wallet_flags=flags, early_buyers=early, top_holders=top[:25], profiles=profiles,
               generated=time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()))
    os.makedirs("docs", exist_ok=True)
    with open(f"docs/{symbol}_{TOKEN[:8]}.json", "w") as fh: json.dump(out, fh, indent=2, default=str)
    with open("docs/latest.json", "w") as fh: json.dump(out, fh, indent=2, default=str)
    notify_telegram(out)

    print(f"\nFirst transfer: block {tr[0].get('block_number')} from {addr(tr[0].get('from'))}")
    print("HISTORY TRUNCATED - raise MAX_PAGES" if TRUNCATED else "Full history loaded")
    print(f"Same-balance clusters (4+ wallets): {len(clusters)}")
    for c in clusters[:5]:
        print(f"  ~{c['approx_balance']} x{len(c['wallets'])} wallets")
    print("\n=== EARLY BUYERS (first 15) ===")
    for e in early[:15]:
        print(f"#{e['buy_rank']:>3} {e['wallet']} bought {e['bought']:.0f} sold {e['sold_pct']}% block {e['first_buy_block']}")
    print("\n=== TOP HOLDERS (first 10) ===")
    for t in top[:10]:
        print(f"{t['wallet']} bal {t['balance']:.0f} early_rank {t['early_rank']}")

def notify_telegram(out):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat): return  # Telegram not set up - skip silently
    e = out.get("early_buyers") or []
    snipers = [x for x, fl in (out.get("wallet_flags") or {}).items() if any(f.startswith("sniper") for f in fl)]
    shared = out.get("shared_funders") or {}
    lines = [f"Scan done: {out.get('symbol')} ({out.get('token','')[:10]}...)",
             f"{out.get('transfers_read')} transfers read, {len(e)} early buyers",
             f"{len(snipers)} sniper-timed buyers, {len(shared)} shared-funder group(s)"]
    if e:
        top = sorted(e, key=lambda x: x['buy_rank'])[:5]
        lines.append("Top early buyers:")
        for b in top:
            lines.append(f"  #{b['buy_rank']} {b['wallet'][:10]}... sold {b.get('sold_pct','–')}%")
    lines.append("Full detail: open the dashboard app.")
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                       json={"chat_id": chat, "text": "\n".join(lines)}, timeout=15)
    except Exception as e:
        print(f"! telegram notify failed: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
