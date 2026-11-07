"""Invite codes: the master makes a short-lived code, a helper pastes it to join.

Flow (endpoints in coordinator/main.py):
    1. POST /invite -> invites.create_invite()               -> {"invite_code", "expires_at"}
    2. Helper pastes the code. Its agent can read the master's address with decode(code).
    3. POST /join   -> invites.validate_and_use_invite(code) -> True/False,
                       then registry.register_node(...) hands the helper its node_id + credentials.
Codes are reusable by several helpers until they expire.
"""
import base64, json, os, time, secrets, threading
from datetime import datetime

DEFAULT_TTL_MIN=15
DEFAULT_PORT=8000
_invites={}      # invite token -> expiry time (unix seconds)
_lock=threading.Lock()


class InviteError(Exception): pass


def create_invite(master_ip=None, port=DEFAULT_PORT, ttl_min=DEFAULT_TTL_MIN):
    """Make a new invite code. master_ip defaults to the LOWLLAMA_MASTER_IP environment variable."""
    master_ip=master_ip or os.environ.get("LOWLLAMA_MASTER_IP")   # master's Tailscale IP, e.g. 100.x.x.x
    token=secrets.token_urlsafe(16)
    expires=int(time.time()+ttl_min*60)
    with _lock: _invites[token]=expires
    payload={"master_ip":master_ip,"port":port,"token":token,"expires":expires}
    code=base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    expires_at=datetime.fromtimestamp(expires).astimezone().isoformat(timespec="seconds")   # e.g. 2026-11-07T17:15:00-05:00
    return {"invite_code":code,"expires_at":expires_at}


def validate_and_use_invite(code):
    """True if this master issued the code and it hasn't expired. The code stays usable for other helpers."""
    try: validate(code); return True
    except InviteError: return False


def decode(code):
    """Read what's inside a code (master address, port). Does NOT check if it's valid."""
    try: payload=json.loads(base64.urlsafe_b64decode(str(code).strip().encode()))
    except Exception: raise InviteError("invite code is not readable")
    if not isinstance(payload,dict): raise InviteError("invite code is not readable")   # e.g. "MTIz" decodes to 123, not an object
    return payload


def validate(code):
    """Check a code against what this master issued. Returns its contents or raises InviteError (says why)."""
    payload=decode(code)
    with _lock: expires=_invites.get(payload.get("token"))
    if expires is None: raise InviteError("invite code was not issued by this master")
    if time.time()>expires: raise InviteError("invite code expired")   # trust our stored expiry, not the code's
    return payload


def cancel_invite(code):
    """Kill a code before it expires. False if it wasn't active."""
    try: token=decode(code).get("token")
    except InviteError: return False
    with _lock: return _invites.pop(token,None) is not None


def clear():
    with _lock: _invites.clear()
