
from __future__ import annotations

import copy
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone

from passwords import hash_password, verify_password

STAGE = 2

MAX_AMOUNT = 1_000_000_000


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str | None = None):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code.replace("_", " ")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def format_ts(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    return dt.isoformat(timespec="microseconds")


def parse_ts(value, *, what: str = "timestamp") -> datetime:
    if not isinstance(value, str) or "T" not in value:
        raise ApiError(422, "validation_failed", f"invalid {what}")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ApiError(422, "validation_failed", f"invalid {what}") from exc
    if parsed.tzinfo is None:
        raise ApiError(422, "validation_failed", f"invalid {what}")
    return parsed.astimezone(timezone.utc)


def canonical(body) -> str:
    return json.dumps(_norm(body), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _norm(value):
    if isinstance(value, dict):
        return {str(k): _norm(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_norm(v) for v in value]
    if isinstance(value, float) and value.is_integer() and abs(value) < 2**53:
        return int(value)
    return value


def json_amount(value, *, minimum: int = 1, maximum: int = MAX_AMOUNT):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    if value < minimum or value > maximum:
        return None
    return value


def equal_split(amount: int, count: int) -> list[int]:
    base, remainder = divmod(amount, count)
    return [base + (1 if i < remainder else 0) for i in range(count)]


def _freeze(value):
    return json.loads(json.dumps(value))


class Ledger:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = None
        self.reset({"currency": "EUR", "minor_units": 2, "users": []})

    def _stamp(self) -> str:
        current = _now()
        previous = self.state.get("clock")
        if previous is not None:
            prev = parse_ts(previous)
            if current <= prev:
                current = prev + timedelta(microseconds=1)
        text = format_ts(current)
        self.state["clock"] = text
        return text

    def _user(self, user_id: str) -> dict:
        user = self.state["users"].get(user_id)
        if user is None:
            raise ApiError(404, "not_found", "no such user")
        return user

    def _by_handle(self, handle: str):
        if not isinstance(handle, str):
            return None
        user_id = self.state["by_handle"].get(handle)
        return self.state["users"].get(user_id) if user_id else None

    def _require_handle(self, handle: str) -> dict:
        if not isinstance(handle, str):
            raise ApiError(400, "malformed_request", "handle must be a string")
        user = self._by_handle(handle)
        if user is None:
            raise ApiError(404, "not_found", "no such handle")
        return user

    def _bearer(self, header):
        if not header or not header.lower().startswith("bearer "):
            raise ApiError(401, "unauthenticated", "missing bearer token")
        token = header.split(" ", 1)[1].strip()
        if not token:
            raise ApiError(401, "unauthenticated", "missing bearer token")
        user_id = self.state["tokens"].get(token)
        if user_id is None or user_id not in self.state["users"]:
            raise ApiError(401, "unauthenticated", "unknown token")
        return self.state["users"][user_id]

    def _expire(self) -> None:
        if STAGE < 2:
            return
        now = _now()
        for auth in self.state["authorizations"].values():
            if auth["status"] != "open":
                continue
            if parse_ts(auth["expires_at"]) <= now:
                holder = self.state["users"][auth["from_user_id"]]
                holder["held"] -= auth["remaining_amount"]
                auth["remaining_amount"] = 0
                auth["status"] = "expired"
                auth["closed_at"] = auth["expires_at"]

    def _available(self, user: dict) -> int:
        if STAGE < 2:
            return user["balance"]
        return user["balance"] - user["held"]

    def _check_invariants(self) -> None:
        total = sum(user["balance"] for user in self.state["users"].values())
        if total != self.state["seeded_total"]:
            raise RuntimeError(f"conservation broken: {total} != {self.state['seeded_total']}")
        for user in self.state["users"].values():
            if user["balance"] < 0 or user["held"] < 0 or user["held"] > user["balance"]:
                raise RuntimeError(f"negative funds for {user['handle']}")

    def _latest_amount(self, payment: dict) -> int:
        if payment.get("revisions"):
            return payment["revisions"][-1]["amount"]
        return payment["receipt"]["amount"]

    def _me(self, user: dict, *, as_of=None, known_at=None) -> dict:
        if STAGE >= 3 and (as_of is not None or known_at is not None):
            as_of_dt = parse_ts(as_of, what="as_of") if as_of is not None else _now()
            known_dt = parse_ts(known_at, what="known_at") if known_at is not None else _now()
            total = self._historical_total(user["id"], as_of_dt, known_dt)
            held = self._historical_held(user["id"], as_of_dt, known_dt) if STAGE >= 2 else 0
        else:
            total = user["balance"]
            held = user["held"] if STAGE >= 2 else 0
        body = {
            "user_id": user["id"],
            "display_name": user["display_name"],
            "handle": user["handle"],
            "balance": total,
            "currency": self.state["currency"],
            "minor_units": self.state["minor_units"],
        }
        if STAGE >= 2:
            body["total"] = total
            body["available"] = total - held
            body["held"] = held
        if as_of is not None:
            body["as_of"] = as_of
        if known_at is not None:
            body["known_at"] = known_at
        return body

    def _request_view(self, item: dict) -> dict:
        return {
            "request_id": item["id"],
            "requester_id": item["requester_id"],
            "requester_handle": self.state["users"][item["requester_id"]]["handle"],
            "payer_id": item["payer_id"],
            "payer_handle": self.state["users"][item["payer_id"]]["handle"],
            "amount": item["amount"],
            "currency": self.state["currency"],
            "note": item["note"],
            "status": item["status"],
            "payment_id": item["payment_id"],
            "created_at": item["created_at"],
        }

    def _auth_view(self, auth: dict) -> dict:
        return {
            "authorization_id": auth["id"],
            "from_user_id": auth["from_user_id"],
            "from_handle": self.state["users"][auth["from_user_id"]]["handle"],
            "to_user_id": auth["to_user_id"],
            "to_handle": self.state["users"][auth["to_user_id"]]["handle"],
            "amount": auth["amount"],
            "captured_amount": auth["captured_amount"],
            "remaining_amount": auth["remaining_amount"],
            "currency": self.state["currency"],
            "note": auth["note"],
            "visibility": auth["visibility"],
            "status": auth["status"],
            "expires_at": auth["expires_at"],
            "payment_id": auth["payment_id"],
            "payment_ids": list(auth["payment_ids"]),
            "created_at": auth["created_at"],
            "closed_at": auth["closed_at"],
        }

    def _make_receipt(self, *, payment_id, sender, receiver, amount, note, visibility,
                      request_id, created_at, settlement_id=None, authorization_id=None,
                      refund_of=None) -> dict:
        receipt = {
            "payment_id": payment_id,
            "from_user_id": sender["id"],
            "from_handle": sender["handle"],
            "to_user_id": receiver["id"],
            "to_handle": receiver["handle"],
            "amount": amount,
            "currency": self.state["currency"],
            "note": note,
            "visibility": visibility,
            "request_id": request_id,
            "created_at": created_at,
            "settlement_id": settlement_id,
        }
        if STAGE >= 2:
            receipt["authorization_id"] = authorization_id
        if STAGE >= 4:
            receipt["refund_of"] = refund_of
        return receipt

    def _idem_slot(self, user_id, method, path, key) -> str:
        return "\n".join((user_id, method, path, key))

    def _begin_idem(self, user, method, path, header, body):
        if header is None or header == "":
            raise ApiError(400, "missing_idempotency_key", "idempotency key required")
        if len(header) > 255:
            raise ApiError(422, "validation_failed", "idempotency key length")
        canon = canonical(body)
        slot = self._idem_slot(user["id"], method, path, header)
        existing = self.state["idempotency"].get(slot)
        if existing is not None:
            if existing["canon"] == canon:
                return existing["response"], None
            raise ApiError(409, "idempotency_key_reuse", "key already used with a different body")
        return None, (slot, canon)

    def _commit_idem(self, pending, response) -> None:
        if pending is None:
            return
        slot, canon = pending
        self.state["idempotency"][slot] = {"canon": canon, "response": _freeze(response)}

    def _note(self, body: dict) -> str:
        if "note" not in body:
            return ""
        note = body["note"]
        if not isinstance(note, str):
            raise ApiError(422, "validation_failed", "note must be a string")
        if len(note) > 200:
            raise ApiError(422, "validation_failed", "note is too long")
        return note

    def _visibility(self, body: dict) -> str:
        if "visibility" not in body:
            return "public"
        visibility = body["visibility"]
        if visibility not in ("public", "private"):
            raise ApiError(422, "validation_failed", "visibility must be public or private")
        return visibility

    def _require_amount(self, body: dict, field: str = "amount", *, minimum: int = 1):
        if field not in body:
            raise ApiError(422, "validation_failed", f"missing {field}")
        amount = json_amount(body[field], minimum=minimum)
        if amount is None:
            raise ApiError(422, "validation_failed", f"invalid {field}")
        return amount

    def _add_payment(self, *, sender, receiver, amount, note, visibility, request_id=None,
                     settlement_id=None, authorization_id=None, refund_of=None, created_at=None):
        created = created_at or self._stamp()
        payment_id = "p_" + uuid.uuid4().hex[:16]
        receipt = self._make_receipt(
            payment_id=payment_id, sender=sender, receiver=receiver, amount=amount,
            note=note, visibility=visibility, request_id=request_id, created_at=created,
            settlement_id=settlement_id, authorization_id=authorization_id, refund_of=refund_of,
        )
        payment = {
            "id": payment_id,
            "from_user_id": sender["id"],
            "to_user_id": receiver["id"],
            "note": note,
            "visibility": visibility,
            "request_id": request_id,
            "settlement_id": settlement_id,
            "authorization_id": authorization_id,
            "refund_of": refund_of,
            "created_at": created,
            "receipt": receipt,
            "refunded": 0,
            "revisions": [{
                "revision": 1,
                "amount": amount,
                "effective_at": created,
                "recorded_at": created,
                "reason": "",
                "correction_batch_id": None,
            }],
        }
        self.state["payments"][payment_id] = payment
        self.state["payment_order"].append(payment_id)
        return payment

    def _move(self, sender, receiver, amount: int, **kwargs):
        if amount:
            if self._available(sender) < amount:
                raise ApiError(409, "insufficient_funds", "not enough available funds")
            sender["balance"] -= amount
            receiver["balance"] += amount
        return self._add_payment(sender=sender, receiver=receiver, amount=amount, **kwargs)

    def signup(self, body: dict):
        email = body.get("email")
        password = body.get("password")
        display = body.get("display_name")
        if not isinstance(email, str) or not isinstance(password, str) or not isinstance(display, str):
            raise ApiError(400, "malformed_request", "signup fields have the wrong type")
        if email.count("@") != 1 or any(part == "" or any(ch.isspace() for ch in part) for part in email.split("@")):
            raise ApiError(422, "validation_failed", "email must look like local@domain")
        if len(password) < 8:
            raise ApiError(422, "validation_failed", "password must be at least 8 characters")
        local = email.split("@", 1)[0].lower()
        handle = "".join(ch if ("a" <= ch <= "z" or ch.isdigit() or ch == "_") else "_" for ch in local)[:20]
        if not handle:
            raise ApiError(422, "validation_failed", "email local part is empty")
        key = email.lower()
        if key in self.state["by_email"]:
            raise ApiError(409, "email_taken", "email already registered")
        if handle in self.state["by_handle"]:
            raise ApiError(409, "handle_taken", "derived handle is already taken")
        user = {
            "id": "u_" + uuid.uuid4().hex[:16],
            "email": key,
            "password": hash_password(password),
            "display_name": display,
            "handle": handle,
            "balance": 0,
            "held": 0,
            "opening": 0,
        }
        self.state["users"][user["id"]] = user
        self.state["by_email"][key] = user["id"]
        self.state["by_handle"][handle] = user["id"]
        self.state["openings"][user["id"]] = 0
        token = uuid.uuid4().hex
        self.state["tokens"][token] = user["id"]
        return 201, {"user_id": user["id"], "display_name": display, "token": token}

    def login(self, body: dict):
        email = body.get("email")
        password = body.get("password")
        if not isinstance(email, str) or not isinstance(password, str):
            raise ApiError(400, "malformed_request", "login fields have the wrong type")
        user_id = self.state["by_email"].get(email.lower())
        user = self.state["users"].get(user_id) if user_id else None
        if user is None or not verify_password(password, user["password"]):
            raise ApiError(401, "unauthenticated", "wrong email or password")
        token = uuid.uuid4().hex
        self.state["tokens"][token] = user["id"]
        return 200, {"user_id": user["id"], "display_name": user["display_name"], "token": token}

    def me(self, user: dict, query: dict):
        as_of = known_at = None
        if STAGE >= 3:
            if "as_of" in query:
                if query["as_of"] == "":
                    raise ApiError(422, "validation_failed", "empty as_of")
                parse_ts(query["as_of"], what="as_of")
                as_of = query["as_of"]
            if "known_at" in query:
                if query["known_at"] == "":
                    raise ApiError(422, "validation_failed", "empty known_at")
                parse_ts(query["known_at"], what="known_at")
                known_at = query["known_at"]
        return 200, self._me(user, as_of=as_of, known_at=known_at)

    def _page(self, query: dict):
        limit = self._query_int(query, "limit", default=50, minimum=1, maximum=200)
        offset = self._query_int(query, "offset", default=0, minimum=0, maximum=None)
        return limit, offset

    def _query_int(self, query, name, *, default, minimum, maximum):
        if name not in query:
            return default
        raw = query[name]
        if not isinstance(raw, str) or not raw.isdigit():
            raise ApiError(422, "validation_failed", f"invalid {name}")
        value = int(raw)
        if value < minimum or (maximum is not None and value > maximum):
            raise ApiError(422, "validation_failed", f"{name} out of range")
        return value

    def list_requests(self, user: dict, query: dict):
        direction = query.get("direction")
        status = query.get("status")
        if direction is not None and direction not in ("incoming", "outgoing"):
            raise ApiError(422, "validation_failed", "invalid direction")
        if status is not None and status not in ("pending", "paid", "declined", "cancelled"):
            raise ApiError(422, "validation_failed", "invalid status")
        limit, offset = self._page(query)
        rows = []
        for request_id in self.state["request_order"]:
            item = self.state["requests"][request_id]
            incoming = item["payer_id"] == user["id"]
            outgoing = item["requester_id"] == user["id"]
            if not incoming and not outgoing:
                continue
            if direction == "incoming" and not incoming:
                continue
            if direction == "outgoing" and not outgoing:
                continue
            if status is not None and item["status"] != status:
                continue
            rows.append(self._request_view(item))
        rows.sort(key=lambda row: (parse_ts(row["created_at"]), row["request_id"]), reverse=True)
        chunk = rows[offset:offset + limit]
        return 200, {"requests": chunk, "has_more": offset + limit < len(rows)}

    def activity(self, user: dict, query: dict):
        limit, offset = self._page(query)
        visible = []
        for payment_id in self.state["payment_order"]:
            payment = self.state["payments"][payment_id]
            receipt = payment["receipt"]
            party = user["id"] in (payment["from_user_id"], payment["to_user_id"])
            if receipt["visibility"] == "public" or party:
                visible.append(receipt)
        visible.sort(key=lambda row: (parse_ts(row["created_at"]), row["payment_id"]), reverse=True)
        chunk = visible[offset:offset + limit]
        return 200, {"payments": chunk, "has_more": offset + limit < len(visible)}

    def create_payment(self, user, body, header):
        replay, pending = self._begin_idem(user, "POST", "/payments", header, body)
        if replay is not None:
            return 200, replay
        amount = self._require_amount(body)
        note = self._note(body)
        visibility = self._visibility(body)
        if "to_handle" not in body:
            raise ApiError(422, "validation_failed", "missing to_handle")
        target = self._require_handle(body["to_handle"])
        if target["id"] == user["id"]:
            raise ApiError(422, "self_payment", "cannot pay yourself")
        payment = self._move(user, target, amount, note=note, visibility=visibility)
        self._commit_idem(pending, payment["receipt"])
        self._check_invariants()
        return 201, payment["receipt"]

    def _new_request(self, requester, payer, amount, note, created=None):
        item = {
            "id": "rq_" + uuid.uuid4().hex[:16],
            "requester_id": requester["id"],
            "payer_id": payer["id"],
            "amount": amount,
            "note": note,
            "status": "pending",
            "payment_id": None,
            "created_at": created or self._stamp(),
        }
        self.state["requests"][item["id"]] = item
        self.state["request_order"].append(item["id"])
        return item

    def create_request(self, user, body, header):
        replay, pending = self._begin_idem(user, "POST", "/requests", header, body)
        if replay is not None:
            return 200, replay
        amount = self._require_amount(body)
        note = self._note(body)
        if "payer_handle" not in body:
            raise ApiError(422, "validation_failed", "missing payer_handle")
        payer = self._require_handle(body["payer_handle"])
        if payer["id"] == user["id"]:
            raise ApiError(422, "self_request", "cannot request from yourself")
        view = self._request_view(self._new_request(user, payer, amount, note))
        self._commit_idem(pending, view)
        return 201, view

    def pay_request(self, user, request_id, body, header):
        path = f"/requests/{request_id}/pay"
        replay, pending = self._begin_idem(user, "POST", path, header, body)
        if replay is not None:
            return 200, replay
        item = self.state["requests"].get(request_id)
        if item is None:
            raise ApiError(404, "not_found", "no such request")
        if item["payer_id"] != user["id"]:
            raise ApiError(403, "forbidden", "only the payer can pay")
        visibility = self._visibility(body)
        if item["status"] != "pending":
            raise ApiError(409, "request_not_pending", "request is not pending")
        requester = self._user(item["requester_id"])
        payment = self._move(
            user, requester, item["amount"], note=item["note"], visibility=visibility, request_id=item["id"],
        )
        item["status"] = "paid"
        item["payment_id"] = payment["id"]
        self._commit_idem(pending, payment["receipt"])
        self._check_invariants()
        return 201, payment["receipt"]

    def decline_request(self, user, request_id):
        item = self.state["requests"].get(request_id)
        if item is None:
            raise ApiError(404, "not_found", "no such request")
        if item["payer_id"] != user["id"]:
            raise ApiError(403, "forbidden", "only the payer can decline")
        if item["status"] == "declined":
            return 200, self._request_view(item)
        if item["status"] != "pending":
            raise ApiError(409, "request_not_pending", "request is not pending")
        item["status"] = "declined"
        return 200, self._request_view(item)

    def cancel_request(self, user, request_id):
        item = self.state["requests"].get(request_id)
        if item is None:
            raise ApiError(404, "not_found", "no such request")
        if item["requester_id"] != user["id"]:
            raise ApiError(403, "forbidden", "only the requester can cancel")
        if item["status"] == "cancelled":
            return 200, self._request_view(item)
        if item["status"] != "pending":
            raise ApiError(409, "request_not_pending", "request is not pending")
        item["status"] = "cancelled"
        return 200, self._request_view(item)

    def create_split(self, user, body, header):
        replay, pending = self._begin_idem(user, "POST", "/splits", header, body)
        if replay is not None:
            return 200, replay
        amount = self._require_amount(body)
        note = self._note(body)
        if "participant_handles" not in body:
            raise ApiError(422, "validation_failed", "missing participant_handles")
        handles = body["participant_handles"]
        if not isinstance(handles, list) or any(not isinstance(handle, str) for handle in handles):
            raise ApiError(400, "malformed_request", "participant_handles must be a list of strings")
        if not handles or len(set(handles)) != len(handles):
            raise ApiError(422, "validation_failed", "participants must be present and unique")
        people = []
        for handle in handles:
            found = self._by_handle(handle)
            if found is None:
                raise ApiError(404, "not_found", "no such handle")
            people.append(found)
        shares = equal_split(amount, len(people))
        created = self._stamp()
        requests = []
        for person, share in zip(people, shares):
            if person["id"] == user["id"]:
                continue
            item = {
                "id": "rq_" + uuid.uuid4().hex[:16],
                "requester_id": user["id"],
                "payer_id": person["id"],
                "amount": share,
                "note": note,
                "status": "pending",
                "payment_id": None,
                "created_at": created,
            }
            self.state["requests"][item["id"]] = item
            self.state["request_order"].append(item["id"])
            requests.append(self._request_view(item))
        response = {
            "split_id": "sp_" + uuid.uuid4().hex[:16],
            "amount": amount,
            "currency": self.state["currency"],
            "note": note,
            "shares": [{"handle": person["handle"], "amount": share} for person, share in zip(people, shares)],
            "requests": requests,
            "created_at": created,
        }
        self.state["splits"][response["split_id"]] = {"id": response["split_id"]}
        self._commit_idem(pending, response)
        return 201, response

    def create_settlement(self, user, body, header):
        replay, pending = self._begin_idem(user, "POST", "/settlements", header, body)
        if replay is not None:
            return 200, replay
        if user["id"] not in self.state["operators"]:
            raise ApiError(403, "forbidden", "settlement operator required")
        transfers = body.get("transfers")
        if not isinstance(transfers, list) or not 1 <= len(transfers) <= 32:
            raise ApiError(422, "validation_failed", "transfers must contain 1 to 32 objects")
        parsed = []
        for entry in transfers:
            if not isinstance(entry, dict):
                raise ApiError(422, "validation_failed", "transfer must be an object")
            amount = self._require_amount(entry)
            note = self._note(entry)
            visibility = self._visibility(entry)
            if "from_handle" not in entry or "to_handle" not in entry:
                raise ApiError(422, "validation_failed", "transfer handles are required")
            sender = self._require_handle(entry["from_handle"])
            receiver = self._require_handle(entry["to_handle"])
            if sender["id"] == receiver["id"]:
                raise ApiError(422, "self_payment", "settlement transfer to the same wallet")
            parsed.append((sender, receiver, amount, note, visibility))
        delta = {user_id: 0 for user_id in self.state["users"]}
        for sender, receiver, amount, _note, _visibility in parsed:
            delta[sender["id"]] -= amount
            delta[receiver["id"]] += amount
        for user_id, change in delta.items():
            if self._available(self.state["users"][user_id]) + change < 0:
                raise ApiError(409, "insufficient_funds", "settlement is not affordable")
        for user_id, change in delta.items():
            self.state["users"][user_id]["balance"] += change
        committed = self._stamp()
        settlement_id = "st_" + uuid.uuid4().hex[:16]
        payments = []
        for sender, receiver, amount, note, visibility in parsed:
            payment = self._add_payment(
                sender=sender, receiver=receiver, amount=amount, note=note,
                visibility=visibility, settlement_id=settlement_id, created_at=committed,
            )
            payments.append(payment["receipt"])
        response = {"settlement_id": settlement_id, "committed_at": committed, "payments": payments}
        self.state["settlements"][settlement_id] = {
            "id": settlement_id,
            "payment_ids": [payment["payment_id"] for payment in payments],
            "committed_at": committed,
        }
        self._commit_idem(pending, response)
        self._check_invariants()
        return 201, response

    def create_authorization(self, user, body, header):
        if STAGE < 2:
            raise ApiError(404, "not_found", "unknown route")
        replay, pending = self._begin_idem(user, "POST", "/authorizations", header, body)
        if replay is not None:
            return 200, replay
        amount = self._require_amount(body)
        note = self._note(body)
        visibility = self._visibility(body)
        if "to_handle" not in body:
            raise ApiError(422, "validation_failed", "missing to_handle")
        target = self._require_handle(body["to_handle"])
        if target["id"] == user["id"]:
            raise ApiError(422, "self_payment", "cannot authorize yourself")
        if self._available(user) < amount:
            raise ApiError(409, "insufficient_funds", "not enough available funds")
        created = self._stamp()
        expires = format_ts(parse_ts(created) + timedelta(seconds=self.state["authorization_ttl"]))
        auth = {
            "id": "a_" + uuid.uuid4().hex[:16],
            "from_user_id": user["id"],
            "to_user_id": target["id"],
            "amount": amount,
            "note": note,
            "visibility": visibility,
            "status": "open",
            "expires_at": expires,
            "created_at": created,
            "closed_at": None,
            "captured_amount": 0,
            "remaining_amount": amount,
            "payment_id": None,
            "payment_ids": [],
            "captures": [],
            "lifecycle": True,
        }
        user["held"] += amount
        self.state["authorizations"][auth["id"]] = auth
        self.state["auth_order"].append(auth["id"])
        view = self._auth_view(auth)
        self._commit_idem(pending, view)
        self._check_invariants()
        return 201, view

    def capture_authorization(self, user, auth_id, body, header):
        if STAGE < 2:
            raise ApiError(404, "not_found", "unknown route")
        path = f"/authorizations/{auth_id}/capture"
        replay, pending = self._begin_idem(user, "POST", path, header, body)
        if replay is not None:
            return 200, replay
        auth = self.state["authorizations"].get(auth_id)
        if auth is None:
            raise ApiError(404, "not_found", "no such authorization")
        if auth["to_user_id"] != user["id"]:
            raise ApiError(403, "forbidden", "only the receiver can capture")
        if auth["status"] == "expired" or (auth["status"] == "open" and parse_ts(auth["expires_at"]) <= _now()):
            raise ApiError(409, "authorization_expired", "authorization has expired")
        if auth["status"] != "open":
            raise ApiError(409, "authorization_not_open", "authorization is not open")
        if "amount" in body:
            amount = json_amount(body["amount"], minimum=1, maximum=MAX_AMOUNT)
            if amount is None:
                raise ApiError(422, "validation_failed", "invalid capture amount")
        else:
            amount = auth["remaining_amount"]
        final = True
        if "final" in body:
            if not isinstance(body["final"], bool):
                raise ApiError(400, "malformed_request", "final must be a boolean")
            final = body["final"]
        if amount > auth["remaining_amount"]:
            raise ApiError(422, "capture_exceeds_authorization", "capture exceeds the remainder")
        sender = self._user(auth["from_user_id"])
        sender["held"] -= amount
        auth["remaining_amount"] -= amount
        auth["captured_amount"] += amount
        close = final or auth["remaining_amount"] == 0
        if close and auth["remaining_amount"]:
            sender["held"] -= auth["remaining_amount"]
            auth["remaining_amount"] = 0
        sender["balance"] -= amount
        user["balance"] += amount
        created = self._stamp()
        payment = self._add_payment(
            sender=sender, receiver=user, amount=amount, note=auth["note"],
            visibility=auth["visibility"], authorization_id=auth["id"], created_at=created,
        )
        auth["captures"].append({"amount": amount, "at": created, "payment_id": payment["id"], "final": close})
        auth["payment_ids"].append(payment["id"])
        auth["payment_id"] = payment["id"]
        if close:
            auth["status"] = "captured"
            auth["closed_at"] = created
            auth["remaining_amount"] = 0
        self._commit_idem(pending, payment["receipt"])
        self._check_invariants()
        return 201, payment["receipt"]

    def void_authorization(self, user, auth_id):
        if STAGE < 2:
            raise ApiError(404, "not_found", "unknown route")
        auth = self.state["authorizations"].get(auth_id)
        if auth is None:
            raise ApiError(404, "not_found", "no such authorization")
        if auth["from_user_id"] != user["id"]:
            raise ApiError(403, "forbidden", "only the payer can void")
        self._expire()
        auth = self.state["authorizations"][auth_id]
        if auth["status"] == "voided":
            return 200, self._auth_view(auth)
        if auth["status"] != "open":
            raise ApiError(409, "authorization_not_open", "authorization is not open")
        holder = self._user(auth["from_user_id"])
        holder["held"] -= auth["remaining_amount"]
        auth["remaining_amount"] = 0
        auth["status"] = "voided"
        auth["closed_at"] = self._stamp()
        self._check_invariants()
        return 200, self._auth_view(auth)

    def list_authorizations(self, user, query):
        if STAGE < 2:
            raise ApiError(404, "not_found", "unknown route")
        direction = query.get("direction")
        status = query.get("status")
        if direction is not None and direction not in ("incoming", "outgoing"):
            raise ApiError(422, "validation_failed", "invalid direction")
        if status is not None and status not in ("open", "captured", "voided", "expired"):
            raise ApiError(422, "validation_failed", "invalid status")
        limit, offset = self._page(query)
        rows = []
        for auth_id in self.state["auth_order"]:
            auth = self.state["authorizations"][auth_id]
            outgoing = auth["from_user_id"] == user["id"]
            incoming = auth["to_user_id"] == user["id"]
            if not incoming and not outgoing:
                continue
            if direction == "outgoing" and not outgoing:
                continue
            if direction == "incoming" and not incoming:
                continue
            if status is not None and auth["status"] != status:
                continue
            rows.append(self._auth_view(auth))
        rows.sort(key=lambda row: (parse_ts(row["created_at"]), row["authorization_id"]), reverse=True)
        chunk = rows[offset:offset + limit]
        return 200, {"authorizations": chunk, "has_more": offset + limit < len(rows)}

    def _selected_revision(self, payment, known):
        chosen = None
        for revision in payment.get("revisions") or []:
            if parse_ts(revision["recorded_at"]) <= known:
                chosen = revision
        return chosen

    def _historical_total(self, user_id, as_of, known) -> int:
        user = self.state["users"][user_id]
        balance = user.get("opening", 0)
        for payment in self.state["payments"].values():
            revision = self._selected_revision(payment, known)
            if revision is None or parse_ts(revision["effective_at"]) > as_of:
                continue
            if payment["from_user_id"] == user_id:
                balance -= revision["amount"]
            elif payment["to_user_id"] == user_id:
                balance += revision["amount"]
        return balance

    def _remaining_at(self, auth, as_of, known) -> int:
        if not auth.get("lifecycle"):
            return 0
        created = parse_ts(auth["created_at"])
        if created > known or created > as_of:
            return 0
        remaining = auth["amount"]
        events = []
        for capture in auth.get("captures") or []:
            at = parse_ts(capture["at"])
            if at <= known:
                events.append((at, 0, capture))
        if auth.get("status") == "voided" and auth.get("closed_at"):
            at = parse_ts(auth["closed_at"])
            if at <= known:
                events.append((at, 1, None))
        events.append((parse_ts(auth["expires_at"]), 2, None))
        events.sort(key=lambda item: (item[0], item[1]))
        for at, kind, capture in events:
            if at > as_of:
                break
            if kind == 0:
                remaining -= capture["amount"]
                if capture["final"] or remaining <= 0:
                    remaining = 0
            else:
                remaining = 0
        return max(0, remaining)

    def _historical_held(self, user_id, as_of, known) -> int:
        return sum(
            self._remaining_at(auth, as_of, known)
            for auth in self.state["authorizations"].values()
            if auth["from_user_id"] == user_id
        )

    def _boundaries(self, known):
        times = set()
        for payment in self.state["payments"].values():
            revision = self._selected_revision(payment, known)
            if revision is not None:
                times.add(parse_ts(revision["effective_at"]))
        for auth in self.state["authorizations"].values():
            if not auth.get("lifecycle"):
                continue
            created = parse_ts(auth["created_at"])
            if created <= known:
                times.add(created)
                times.add(parse_ts(auth["expires_at"]))
                for capture in auth.get("captures") or []:
                    at = parse_ts(capture["at"])
                    if at <= known:
                        times.add(at)
                if auth.get("status") == "voided" and auth.get("closed_at"):
                    at = parse_ts(auth["closed_at"])
                    if at <= known:
                        times.add(at)
        return sorted(times)

    def _history_ok(self, known=None) -> bool:
        known = known or _now()
        for moment in self._boundaries(known):
            for user_id in self.state["users"]:
                total = self._historical_total(user_id, moment, known)
                held = self._historical_held(user_id, moment, known) if STAGE >= 2 else 0
                if total < 0 or total - held < 0:
                    return False
        return True

    def statement(self, user, query):
        if STAGE < 3:
            raise ApiError(404, "not_found", "unknown route")
        if "snapshot" in query:
            if any(name in query for name in ("from", "to", "known_at")):
                raise ApiError(422, "validation_failed", "snapshot cannot be combined with a window")
            frozen = self.state["snapshots"].get(query["snapshot"])
            if frozen is None or frozen["user_id"] != user["id"]:
                raise ApiError(404, "not_found", "unknown snapshot")
            limit, offset = self._page(query)
            rows = frozen["entries"]
            body = {
                "opening_balance": frozen["opening_balance"],
                "entries": rows[offset:offset + limit],
                "closing_balance": frozen["closing_balance"],
                "has_more": offset + limit < len(rows),
                "snapshot": query["snapshot"],
            }
            if frozen.get("known_at") is not None:
                body["known_at"] = frozen["known_at"]
            return 200, body
        known_at = query.get("known_at")
        if known_at == "":
            raise ApiError(422, "validation_failed", "empty known_at")
        known = parse_ts(known_at, what="known_at") if known_at else _now()
        start = parse_ts(query["from"], what="from") if "from" in query else datetime.min.replace(tzinfo=timezone.utc)
        end = parse_ts(query["to"], what="to") if "to" in query else _now() + timedelta(seconds=1)
        if "from" in query and "to" in query and start > end:
            raise ApiError(422, "validation_failed", "from is after to")
        selected = []
        for payment in self.state["payments"].values():
            if user["id"] not in (payment["from_user_id"], payment["to_user_id"]):
                continue
            revision = self._selected_revision(payment, known)
            if revision is None:
                continue
            selected.append((parse_ts(revision["effective_at"]), payment, revision))
        selected.sort(key=lambda item: (item[0], item[1]["id"]))
        opening = user.get("opening", 0)
        for effective, payment, revision in selected:
            if effective >= start:
                break
            opening += revision["amount"] if payment["to_user_id"] == user["id"] else -revision["amount"]
        closing = opening
        entries = []
        for effective, payment, revision in selected:
            if effective < start or effective >= end:
                continue
            delta = revision["amount"] if payment["to_user_id"] == user["id"] else -revision["amount"]
            closing += delta
            receipt = dict(payment["receipt"])
            receipt["amount"] = revision["amount"]
            entries.append({
                "payment": receipt,
                "delta": delta,
                "balance_after": closing,
                "revision": revision["revision"],
                "effective_at": revision["effective_at"],
                "recorded_at": revision["recorded_at"],
            })
        limit, offset = self._page(query)
        token = "snap_" + uuid.uuid4().hex
        self.state["snapshots"][token] = {
            "user_id": user["id"],
            "opening_balance": opening,
            "closing_balance": closing,
            "entries": entries,
            "known_at": known_at,
        }
        body = {
            "opening_balance": opening,
            "entries": entries[offset:offset + limit],
            "closing_balance": closing,
            "has_more": offset + limit < len(entries),
            "snapshot": token,
        }
        if known_at is not None:
            body["known_at"] = known_at
        return 200, body

    def _correction_item(self, body, *, allow_settlement: bool):
        if not isinstance(body, dict):
            raise ApiError(422, "validation_failed", "correction must be an object")
        payment_id = body.get("payment_id")
        if not isinstance(payment_id, str):
            raise ApiError(422, "validation_failed", "missing payment id")
        payment = self.state["payments"].get(payment_id)
        if payment is None:
            raise ApiError(404, "not_found", "no such payment")
        if payment.get("authorization_id") or payment.get("refund_of"):
            raise ApiError(422, "linked_payment_immutable", "linked payments cannot be corrected")
        if payment.get("settlement_id") and not allow_settlement:
            raise ApiError(422, "linked_payment_immutable", "correct a settlement through a batch")
        if any(name not in body for name in ("expected_revision", "amount", "effective_at", "reason")):
            raise ApiError(422, "validation_failed", "missing correction field")
        expected = body["expected_revision"]
        if isinstance(expected, bool) or not isinstance(expected, (int, float)):
            raise ApiError(422, "validation_failed", "invalid revision")
        if isinstance(expected, float):
            if not expected.is_integer():
                raise ApiError(422, "validation_failed", "invalid revision")
            expected = int(expected)
        if expected < 1:
            raise ApiError(422, "validation_failed", "invalid revision")
        amount = json_amount(body["amount"], minimum=0)
        if amount is None:
            raise ApiError(422, "validation_failed", "invalid amount")
        effective_at = body["effective_at"]
        effective = parse_ts(effective_at, what="effective_at")
        if effective > _now() + timedelta(seconds=2):
            raise ApiError(422, "validation_failed", "effective time is in the future")
        reason = body["reason"]
        if not isinstance(reason, str) or not 1 <= len(reason) <= 200:
            raise ApiError(422, "validation_failed", "invalid reason")
        if expected != payment["revisions"][-1]["revision"]:
            raise ApiError(409, "stale_revision", "revision is stale")
        if amount < payment.get("refunded", 0):
            raise ApiError(422, "refund_exceeds_payment", "correction is below the refunded amount")
        return payment, amount, effective_at, reason

    def _funds_for(self, proposals) -> None:
        delta = {user_id: 0 for user_id in self.state["users"]}
        for payment, amount, _effective, _reason in proposals:
            diff = amount - self._latest_amount(payment)
            delta[payment["from_user_id"]] -= diff
            delta[payment["to_user_id"]] += diff
        for user_id, change in delta.items():
            if change < 0 and self._available(self.state["users"][user_id]) < -change:
                raise ApiError(409, "insufficient_funds", "correction is not affordable now")

    def _apply_correction(self, payment, amount, effective_at, reason, recorded_at, batch_id):
        previous = self._latest_amount(payment)
        diff = amount - previous
        sender = self._user(payment["from_user_id"])
        receiver = self._user(payment["to_user_id"])
        sender["balance"] -= diff
        receiver["balance"] += diff
        revision = {
            "revision": payment["revisions"][-1]["revision"] + 1,
            "amount": amount,
            "effective_at": effective_at,
            "recorded_at": recorded_at,
            "reason": reason,
            "correction_batch_id": batch_id,
        }
        payment["revisions"].append(revision)
        return revision

    def _commit_corrections(self, proposals, recorded_at, batch_id):
        trial = copy.deepcopy(self.state)
        shadow = Ledger.__new__(Ledger)
        shadow.state = trial
        for payment, amount, effective_at, reason in proposals:
            cloned = shadow.state["payments"][payment["id"]]
            shadow._apply_correction(cloned, amount, effective_at, reason, recorded_at, batch_id)
        if not shadow._history_ok(_now()):
            raise ApiError(409, "historical_overdraft", "correction overdraws a past balance")
        return [
            self._apply_correction(payment, amount, effective_at, reason, recorded_at, batch_id)
            for payment, amount, effective_at, reason in proposals
        ]

    def correct_payment(self, user, payment_id, body, header):
        if STAGE < 3:
            raise ApiError(404, "not_found", "unknown route")
        path = f"/payments/{payment_id}/corrections"
        replay, pending = self._begin_idem(user, "POST", path, header, body)
        if replay is not None:
            return 200, replay
        proposal = dict(body)
        proposal["payment_id"] = payment_id
        payment, amount, effective_at, reason = self._correction_item(proposal, allow_settlement=False)
        if payment["from_user_id"] != user["id"]:
            raise ApiError(403, "forbidden", "only the original sender can correct")
        proposals = [(payment, amount, effective_at, reason)]
        self._funds_for(proposals)
        recorded = self._stamp()
        revision = self._commit_corrections(proposals, recorded, None)[0]
        response = {
            "payment_id": payment["id"],
            "revision": revision["revision"],
            "amount": revision["amount"],
            "effective_at": revision["effective_at"],
            "recorded_at": revision["recorded_at"],
            "reason": revision["reason"],
        }
        self._commit_idem(pending, response)
        self._check_invariants()
        return 201, response

    def list_revisions(self, user, payment_id):
        if STAGE < 3:
            raise ApiError(404, "not_found", "unknown route")
        payment = self.state["payments"].get(payment_id)
        if payment is None or user["id"] not in (payment["from_user_id"], payment["to_user_id"]):
            raise ApiError(404, "not_found", "no such payment")
        revisions = []
        for revision in payment["revisions"]:
            row = {
                "revision": revision["revision"],
                "amount": revision["amount"],
                "effective_at": revision["effective_at"],
                "recorded_at": revision["recorded_at"],
                "reason": revision["reason"],
            }
            if STAGE >= 4:
                row["correction_batch_id"] = revision.get("correction_batch_id")
            revisions.append(row)
        return 200, {"revisions": revisions}

    def refund_payment(self, user, payment_id, body, header):
        if STAGE < 4:
            raise ApiError(404, "not_found", "unknown route")
        path = f"/payments/{payment_id}/refunds"
        replay, pending = self._begin_idem(user, "POST", path, header, body)
        if replay is not None:
            return 200, replay
        payment = self.state["payments"].get(payment_id)
        if payment is None:
            raise ApiError(404, "not_found", "no such payment")
        if payment["to_user_id"] != user["id"]:
            raise ApiError(403, "forbidden", "only the receiver can refund")
        if payment.get("refund_of"):
            raise ApiError(422, "invalid_refund_target", "a refund cannot be refunded")
        amount = self._require_amount(body)
        if payment.get("refunded", 0) + amount > self._latest_amount(payment):
            raise ApiError(422, "refund_exceeds_payment", "refund exceeds the corrected amount")
        refund = self._move(
            user, self._user(payment["from_user_id"]), amount,
            note=payment["note"], visibility=payment["visibility"], refund_of=payment["id"],
        )
        payment["refunded"] = payment.get("refunded", 0) + amount
        self._commit_idem(pending, refund["receipt"])
        self._check_invariants()
        return 201, refund["receipt"]

    def correction_batch(self, user, body, header):
        if STAGE < 4:
            raise ApiError(404, "not_found", "unknown route")
        replay, pending = self._begin_idem(user, "POST", "/correction-batches", header, body)
        if replay is not None:
            return 200, replay
        if user["id"] not in self.state["operators"]:
            raise ApiError(403, "forbidden", "settlement operator required")
        items = body.get("corrections")
        if not isinstance(items, list) or not 1 <= len(items) <= 32:
            raise ApiError(422, "validation_failed", "corrections must contain 1 to 32 objects")
        seen = set()
        proposals = []
        for entry in items:
            if isinstance(entry, dict) and entry.get("payment_id") in seen:
                raise ApiError(422, "validation_failed", "duplicate payment in batch")
            if isinstance(entry, dict) and isinstance(entry.get("payment_id"), str):
                seen.add(entry["payment_id"])
            payment, amount, effective_at, reason = self._correction_item(entry, allow_settlement=True)
            proposals.append((payment, amount, effective_at, reason))
        groups = {}
        for payment, _amount, effective_at, _reason in proposals:
            if payment.get("settlement_id"):
                groups.setdefault(payment["settlement_id"], []).append((payment["id"], effective_at))
        for settlement_id, members in groups.items():
            needed = set(self.state["settlements"][settlement_id]["payment_ids"])
            got = {payment_id for payment_id, _effective in members}
            if got != needed:
                raise ApiError(422, "incomplete_settlement", "batch must include every settlement member")
            instants = {parse_ts(effective) for _payment_id, effective in members}
            if len(instants) > 1:
                raise ApiError(422, "validation_failed", "settlement members need one effective instant")
        self._funds_for(proposals)
        recorded = self._stamp()
        for payment, _amount, _effective, _reason in proposals:
            previous = parse_ts(payment["revisions"][-1]["recorded_at"])
            if parse_ts(recorded) <= previous:
                recorded = format_ts(previous + timedelta(microseconds=1))
                self.state["clock"] = recorded
        batch_id = "cb_" + uuid.uuid4().hex[:16]
        revisions = self._commit_corrections(proposals, recorded, batch_id)
        rows = []
        for revision, (payment, _amount, _effective, _reason) in zip(revisions, proposals):
            rows.append({
                "payment_id": payment["id"],
                "revision": revision["revision"],
                "amount": revision["amount"],
                "effective_at": revision["effective_at"],
                "recorded_at": revision["recorded_at"],
                "reason": revision["reason"],
                "correction_batch_id": batch_id,
            })
        response = {"correction_batch_id": batch_id, "recorded_at": recorded, "revisions": rows}
        self.state["batches"][batch_id] = {"id": batch_id}
        self._commit_idem(pending, response)
        self._check_invariants()
        return 201, response

    def reset(self, fixture: dict) -> None:
        self.state = self._build(fixture)

    def _receipt_for(self, *, payment_id, sender, receiver, amount, note, visibility, created, currency):
        receipt = {
            "payment_id": payment_id,
            "from_user_id": sender["id"],
            "from_handle": sender["handle"],
            "to_user_id": receiver["id"],
            "to_handle": receiver["handle"],
            "amount": amount,
            "currency": currency,
            "note": note,
            "visibility": visibility,
            "request_id": None,
            "created_at": created,
            "settlement_id": None,
        }
        if STAGE >= 2:
            receipt["authorization_id"] = None
        if STAGE >= 4:
            receipt["refund_of"] = None
        return receipt

    def _build(self, fixture: dict) -> dict:
        if not isinstance(fixture, dict):
            raise ApiError(422, "validation_failed", "fixture must be an object")
        currency = fixture.get("currency", "EUR")
        minor = fixture.get("minor_units", 2)
        if not isinstance(currency, str) or isinstance(minor, bool) or minor not in (0, 2, 3):
            raise ApiError(422, "validation_failed", "invalid currency")
        ttl = fixture.get("authorization_ttl_seconds", 600)
        if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 1:
            raise ApiError(422, "validation_failed", "invalid authorization lifetime")
        state = {
            "currency": currency,
            "minor_units": minor,
            "authorization_ttl": ttl,
            "users": {},
            "by_email": {},
            "by_handle": {},
            "tokens": {},
            "payments": {},
            "payment_order": [],
            "requests": {},
            "request_order": [],
            "splits": {},
            "settlements": {},
            "authorizations": {},
            "auth_order": [],
            "idempotency": {},
            "operators": [],
            "snapshots": {},
            "batches": {},
            "seeded_total": 0,
            "openings": {},
            "clock": format_ts(_now()),
        }
        now = state["clock"]
        for raw in fixture.get("users") or []:
            if not isinstance(raw, dict):
                raise ApiError(422, "validation_failed", "user must be an object")
            balance = json_amount(raw.get("balance", 0), minimum=0, maximum=2**53)
            if balance is None:
                raise ApiError(422, "validation_failed", "negative or invalid seeded balance")
            handle = raw.get("handle")
            email = raw.get("email")
            if not isinstance(handle, str) or not isinstance(email, str) or not isinstance(raw.get("password"), str):
                raise ApiError(422, "validation_failed", "seeded user is incomplete")
            if handle in state["by_handle"] or email.lower() in state["by_email"] or raw.get("id") in state["users"]:
                raise ApiError(422, "validation_failed", "duplicate seeded user")
            user = {
                "id": raw.get("id") or ("u_" + uuid.uuid4().hex[:16]),
                "email": email.lower(),
                "password": hash_password(raw["password"]),
                "display_name": raw.get("display_name") or handle,
                "handle": handle,
                "balance": balance,
                "held": 0,
                "opening": balance,
            }
            state["users"][user["id"]] = user
            state["by_email"][user["email"]] = user["id"]
            state["by_handle"][handle] = user["id"]
            state["openings"][user["id"]] = balance
            state["seeded_total"] += balance
        for raw in fixture.get("payments") or []:
            if not isinstance(raw, dict):
                raise ApiError(422, "validation_failed", "seeded payment is invalid")
            sender = state["users"].get(raw.get("from_user_id"))
            receiver = state["users"].get(raw.get("to_user_id"))
            amount = json_amount(raw.get("amount"), minimum=0)
            if sender is None or receiver is None or amount is None:
                raise ApiError(422, "validation_failed", "seeded payment is invalid")
            created = raw.get("created_at") or now
            created_at = parse_ts(created)
            if STAGE >= 3 and created_at > _now() + timedelta(seconds=2):
                raise ApiError(422, "validation_failed", "seeded payment is in the future")
            note = raw.get("note") or ""
            visibility = raw.get("visibility") or "public"
            if not isinstance(note, str) or visibility not in ("public", "private"):
                raise ApiError(422, "validation_failed", "seeded payment is invalid")
            payment_id = raw.get("id") or ("p_" + uuid.uuid4().hex[:16])
            receipt = self._receipt_for(
                payment_id=payment_id, sender=sender, receiver=receiver, amount=amount,
                note=note, visibility=visibility, created=created, currency=currency,
            )
            state["payments"][payment_id] = {
                "id": payment_id,
                "from_user_id": sender["id"],
                "to_user_id": receiver["id"],
                "note": note,
                "visibility": visibility,
                "request_id": None,
                "settlement_id": None,
                "authorization_id": None,
                "refund_of": None,
                "created_at": created,
                "receipt": receipt,
                "refunded": 0,
                "revisions": [{
                    "revision": 1, "amount": amount, "effective_at": created,
                    "recorded_at": created, "reason": "", "correction_batch_id": None,
                }],
            }
            state["payment_order"].append(payment_id)
            sender["opening"] += amount
            receiver["opening"] -= amount
            state["openings"][sender["id"]] += amount
            state["openings"][receiver["id"]] -= amount
        for raw in fixture.get("requests") or []:
            if not isinstance(raw, dict):
                raise ApiError(422, "validation_failed", "seeded request is invalid")
            if raw.get("requester_id") not in state["users"] or raw.get("payer_id") not in state["users"]:
                raise ApiError(422, "validation_failed", "seeded request is invalid")
            amount = json_amount(raw.get("amount"), minimum=0)
            status = raw.get("status") or "pending"
            if amount is None or status not in ("pending", "paid", "declined", "cancelled"):
                raise ApiError(422, "validation_failed", "seeded request is invalid")
            item = {
                "id": raw.get("id") or ("rq_" + uuid.uuid4().hex[:16]),
                "requester_id": raw["requester_id"],
                "payer_id": raw["payer_id"],
                "amount": amount,
                "note": raw.get("note") or "",
                "status": status,
                "payment_id": raw.get("payment_id"),
                "created_at": raw.get("created_at") or now,
            }
            state["requests"][item["id"]] = item
            state["request_order"].append(item["id"])
        if STAGE >= 2:
            held = {user_id: 0 for user_id in state["users"]}
            for raw in fixture.get("authorizations") or []:
                if not isinstance(raw, dict):
                    raise ApiError(422, "validation_failed", "seeded authorization is invalid")
                sender = state["users"].get(raw.get("from_user_id"))
                receiver = state["users"].get(raw.get("to_user_id"))
                amount = json_amount(raw.get("amount"), minimum=1)
                status = raw.get("status") or "open"
                if sender is None or receiver is None or amount is None or status not in ("open", "captured", "voided", "expired"):
                    raise ApiError(422, "validation_failed", "seeded authorization is invalid")
                expires = raw.get("expires_at") or format_ts(parse_ts(now) + timedelta(seconds=ttl))
                created = raw.get("created_at") or now
                open_now = status == "open" and parse_ts(expires) > _now()
                if open_now:
                    held[sender["id"]] += amount
                auth = {
                    "id": raw.get("id") or ("a_" + uuid.uuid4().hex[:16]),
                    "from_user_id": sender["id"],
                    "to_user_id": receiver["id"],
                    "amount": amount,
                    "note": raw.get("note") or "",
                    "visibility": raw.get("visibility") or "public",
                    "status": status if open_now or status != "open" else "expired",
                    "expires_at": expires,
                    "created_at": created,
                    "closed_at": None if open_now else (raw.get("closed_at") or expires),
                    "captured_amount": 0 if status == "open" else amount,
                    "remaining_amount": amount if open_now else 0,
                    "payment_id": raw.get("payment_id"),
                    "payment_ids": list(raw.get("payment_ids") or []),
                    "captures": [],
                    "lifecycle": status == "open",
                }
                state["authorizations"][auth["id"]] = auth
                state["auth_order"].append(auth["id"])
            for user_id, amount in held.items():
                if amount > state["users"][user_id]["balance"]:
                    raise ApiError(422, "validation_failed", "seeded holds exceed the balance")
                state["users"][user_id]["held"] = amount
        for operator_id in fixture.get("settlement_operator_ids") or []:
            if operator_id not in state["users"]:
                raise ApiError(422, "validation_failed", "unknown settlement operator")
            state["operators"].append(operator_id)
        for user in state["users"].values():
            if user["opening"] < 0:
                raise ApiError(422, "validation_failed", "seeded history is negative")
        return state

    def export(self) -> dict:
        return {"track": "pocketful", "format_version": 1, "state": _freeze(self.state)}

    def import_state(self, body: dict) -> None:
        if not isinstance(body, dict) or body.get("track") != "pocketful" or body.get("format_version") != 1:
            raise ApiError(422, "validation_failed", "unsupported snapshot")
        raw = body.get("state")
        if not isinstance(raw, dict):
            raise ApiError(422, "validation_failed", "snapshot state is missing")
        self.state = self._hydrate(raw)

    def _hydrate(self, raw: dict) -> dict:
        state = copy.deepcopy(raw)
        state.setdefault("currency", "EUR")
        state.setdefault("minor_units", 2)
        state.setdefault("authorization_ttl", 600)
        state.setdefault("users", {})
        state.setdefault("tokens", {})
        state.setdefault("payments", {})
        state.setdefault("payment_order", list(state["payments"]))
        state.setdefault("requests", {})
        state.setdefault("request_order", list(state["requests"]))
        state.setdefault("splits", {})
        state.setdefault("settlements", {})
        state.setdefault("authorizations", {})
        state.setdefault("auth_order", list(state["authorizations"]))
        state.setdefault("idempotency", {})
        state.setdefault("operators", [])
        state.setdefault("snapshots", {})
        state.setdefault("batches", {})
        state.setdefault("openings", {})
        state.setdefault("clock", None)
        state.setdefault("seeded_total", sum(user.get("balance", 0) for user in state["users"].values()))
        state["by_email"] = {}
        state["by_handle"] = {}
        for user in state["users"].values():
            user.setdefault("held", 0)
            user.setdefault("opening", state["openings"].get(user["id"], user.get("balance", 0)))
            state["openings"][user["id"]] = user["opening"]
            state["by_email"][user["email"]] = user["id"]
            state["by_handle"][user["handle"]] = user["id"]
        for payment in state["payments"].values():
            payment.setdefault("refunded", 0)
            payment.setdefault("authorization_id", None)
            payment.setdefault("refund_of", None)
            payment.setdefault("settlement_id", None)
            payment.setdefault("request_id", None)
            if not payment.get("revisions"):
                created = payment.get("created_at") or payment["receipt"]["created_at"]
                amount = payment["receipt"]["amount"]
                payment["revisions"] = [{
                    "revision": 1, "amount": amount, "effective_at": created,
                    "recorded_at": created, "reason": "", "correction_batch_id": None,
                }]
            if STAGE >= 2 and "authorization_id" not in payment["receipt"]:
                payment["receipt"] = dict(payment["receipt"])
                payment["receipt"]["authorization_id"] = payment.get("authorization_id")
            if STAGE >= 4 and "refund_of" not in payment["receipt"]:
                payment["receipt"] = dict(payment["receipt"])
                payment["receipt"]["refund_of"] = payment.get("refund_of")
        for auth in state["authorizations"].values():
            auth.setdefault("captures", [])
            auth.setdefault("payment_ids", [])
            auth.setdefault("lifecycle", auth.get("status") == "open")
            auth.setdefault("remaining_amount", auth["amount"] if auth.get("status") == "open" else 0)
            auth.setdefault("captured_amount", 0)
            auth.setdefault("closed_at", None)
        return state


LEDGER = Ledger()
