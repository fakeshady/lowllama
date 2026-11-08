"""Invite codes: the master makes a short-lived code, a helper pastes it to join.

Flow (endpoints in coordinator/main.py):
    1. POST /invite -> invites.create_invite()               -> {"invite_code", "expires_at"}
    2. Helper pastes the code. Its agent can read the master's address with decode(code).
    3. POST /join   -> invites.validate_and_use_invite(code) -> True/False,
                       then registry.register_node(...) hands the helper its node_id + credentials.

Rules:
    - Codes are reusable by several helpers until they expire (team policy). validate_and_use_invite only
      checks; it does not use the code up.
    - A code is base64 JSON, which is encoding, NOT encryption: anyone holding it can read the master's
      address and use it to join until it expires. Treat it like a password and never log it.
    - Expiry is checked against what this master stored, never against the expiry written inside the code.
    - Invites only let a laptop join the LowLlama pool. They do not add it to the Tailscale network.
    - Everything lives in memory: restarting the coordinator cancels every code (and forgets every node), so
      helpers need a NEW invite after a coordinator restart. Persistence is a future option, not built.

Errors: every failure raises InviteError (message safe to show, never contains the code or token).
    create_invite without a usable master address -> InviteError -> HTTP 503 (server not configured)
    unreadable / unknown / expired code           -> InviteError -> HTTP 400 or 403
"""
import base64, binascii, json, os, time, secrets, threading, math, ipaddress
from datetime import datetime

DEFAULT_TTL_MIN=15
MAX_TTL_MIN=24*60   # one day
DEFAULT_PORT=8000
MAX_CODE_LEN=1024   # real codes are ~200 characters; anything far longer is junk
_invites={}      # invite token -> expiry time (unix seconds)
_lock=threading.Lock()


class InviteError(Exception): pass


def _now():
    return time.time()   # the coordinator's clock; tests replace this to check exact expiry boundaries


def create_invite(master_ip=None, port=DEFAULT_PORT, ttl_min=DEFAULT_TTL_MIN):
    """Make a new invite code. master_ip defaults to the LOWLLAMA_MASTER_IP environment variable.

    Raises InviteError instead of making a code with a missing or unusable address, port or lifetime.
    """
    master_ip=_clean_master_ip(master_ip if master_ip is not None else os.environ.get("LOWLLAMA_MASTER_IP"))
    port=_clean_port(port)
    ttl_min=_clean_ttl(ttl_min)
    token=secrets.token_urlsafe(16)
    now=_now()
    expires=int(now+ttl_min*60)
    with _lock:
        for old in [t for t,exp in _invites.items() if exp<now]: del _invites[old]   # forget expired codes so the dict can't grow forever
        _invites[token]=expires
    payload={"master_ip":master_ip,"port":port,"token":token,"expires":expires}   # "expires" is informational only
    code=base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    expires_at=datetime.fromtimestamp(expires).astimezone().isoformat(timespec="seconds")   # e.g. 2026-11-07T17:15:00-05:00
    return {"invite_code":code,"expires_at":expires_at}


def validate_and_use_invite(code):
    """True if this master issued the code and it hasn't expired. Validation only: the code stays usable for other helpers."""
    try: validate(code); return True
    except InviteError: return False


def decode(code):
    """Read what's inside a code (master address, port). Does NOT check that this master issued it or that it's unexpired."""
    if not isinstance(code,str): raise InviteError("invite code must be text")
    code=code.strip()
    if not code or len(code)>MAX_CODE_LEN: raise InviteError("invite code is not readable")
    try:
        raw=base64.b64decode(code+"="*(-len(code)%4),altchars=b"-_",validate=True)   # strict: rejects characters outside URL-safe base64
        payload=json.loads(raw)
    except (binascii.Error,ValueError): raise InviteError("invite code is not readable") from None
    if not isinstance(payload,dict) or not isinstance(payload.get("token"),str):
        raise InviteError("invite code is not readable")   # e.g. "MTIz" decodes to 123, not an object
    return payload


def validate(code):
    """Check a code against what this master issued. Returns its contents or raises InviteError (says why)."""
    payload=decode(code)
    with _lock: expires=_invites.get(payload["token"])
    if expires is None: raise InviteError("invite code was not issued by this master, was cancelled, or the master restarted")
    if _now()>expires: raise InviteError("invite code expired")   # trust our stored expiry, not the code's
    return payload


def cancel_invite(code):
    """Kill a code before it expires. False if it wasn't active."""
    try: token=decode(code)["token"]
    except InviteError: return False
    with _lock: return _invites.pop(token,None) is not None


def clear():
    with _lock: _invites.clear()


def _clean_master_ip(master_ip):
    if master_ip is None or (isinstance(master_ip,str) and not master_ip.strip()):
        raise InviteError("master address is not configured; set LOWLLAMA_MASTER_IP to the master's Tailscale IP")
    if not isinstance(master_ip,str): raise InviteError("master address must be an IP address")
    try: ip=ipaddress.ip_address(master_ip.strip())
    except ValueError: raise InviteError("master address must be an IP address, e.g. 100.x.x.x") from None
    if ip.is_unspecified or ip.is_multicast: raise InviteError("master address must be a real, reachable IP (not 0.0.0.0 or multicast)")
    return str(ip)


def _clean_port(port):
    if isinstance(port,bool) or not isinstance(port,int) or not 1<=port<=65535: raise InviteError("port must be a whole number from 1 to 65535")
    return port


def _clean_ttl(ttl_min):
    if isinstance(ttl_min,bool) or not isinstance(ttl_min,(int,float)) or not math.isfinite(ttl_min) or not 0<=ttl_min<=MAX_TTL_MIN:
        raise InviteError(f"ttl_min must be a number from 0 to {MAX_TTL_MIN} minutes")
    return ttl_min
